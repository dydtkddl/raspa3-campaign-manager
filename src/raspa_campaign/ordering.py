"""P04 queue ordering is explicit and independent of task/Monte Carlo seed."""
from __future__ import annotations
import math
from .config import ConfigError
from .discovery import number
from .hashing import canonical_sha256

ORDERS=('runtime_desc','pld_desc','pld_asc','name_asc','name_desc','random','inventory')


def order_policy(config):
    order=config.get('scheduling.order')
    if order is None:return None
    if order not in ORDERS:raise ConfigError('scheduling.order must be one of '+', '.join(ORDERS))
    scope=config.get('scheduling.order_scope','global')
    missing=config.get('scheduling.missing_pld','error')
    if scope not in ('global','gas_priority') or missing not in ('error','last'):
        raise ConfigError('Invalid order_scope or missing_pld policy')
    if scope=='global' and config.get('scheduling.priority_mode','soft') in ('strict','strict-staged','strict_staged'):
        raise ConfigError('Global order conflicts with strict gas stages; choose order_scope=gas_priority or priority_mode=soft')
    if not isinstance(config.get('scheduling.pld_column','pld_A'),str):
        raise ConfigError('scheduling.pld_column must be a string')
    seed=config.get('scheduling.order_seed')
    if order=='random' and (type(seed) is not int or not 0<=seed<=2147483647):
        raise ConfigError('random order requires explicit integer scheduling.order_seed; independent of Monte Carlo seed')
    gas=config.get('scheduling.gas_priority',[])
    if not isinstance(gas,list) or len(gas)!=len(set(gas)) or any(not isinstance(g,str) for g in gas):
        raise ConfigError('scheduling.gas_priority must be a unique string list')
    return {'order':order,'scope':scope,'order_seed':seed if order=='random' else None,
            'pld_column':config.get('scheduling.pld_column','pld_A'),'missing_pld':missing,
            'gas_priority':gas}


def ordered_tasks(tasks, config):
    policy=order_policy(config)
    if policy is None:return None
    if len({t.task_id for t in tasks})!=len(tasks):raise ConfigError('Duplicate task ID in execution order')
    order=policy['order']; ranks={t.task_id:i for i,t in enumerate(tasks)}
    priorities={g:i for i,g in enumerate(policy['gas_priority'])}
    def tie(t):return (t.mof_id.casefold(),t.mof_id,t.gas,t.temperature_K,t.pressure_Pa,t.seed,t.replicate,t.task_id)
    def key(t):
        group=(priorities.get(t.gas,len(priorities)),t.gas) if policy['scope']=='gas_priority' else ()
        if order.startswith('pld_'):
            n=number(t.descriptors.get(policy['pld_column']),f'PLD for {t.mof_id}',allow_missing=True)
            if n is None and policy['missing_pld']=='error':raise ConfigError('Missing PLD for '+t.mof_id+'; provide descriptor CSV or explicitly set missing_pld="last"')
            primary=(n is None,(-n if order=='pld_desc' else n) if n is not None else 0)
        elif order=='runtime_desc':
            if not math.isfinite(t.estimated_seconds) or t.estimated_seconds<0:raise ConfigError('Invalid estimated runtime')
            primary=(-t.estimated_seconds,)
        elif order=='random':primary=(canonical_sha256(['rcm-queue-p04-v1',policy['order_seed'],t.mof_id]),)
        elif order=='inventory':primary=(ranks[t.task_id],)
        elif order=='name_desc':
            # Reverse only the MOF-name rank, not seed, pressure, or tie ordering.
            primary=(name_ranks[(t.mof_id.casefold(),t.mof_id)],)
        else:primary=(t.mof_id.casefold(),t.mof_id)
        return (*group,*primary,*tie(t))
    name_ranks={n:i for i,n in enumerate(sorted({(t.mof_id.casefold(),t.mof_id) for t in tasks},reverse=True))}
    return sorted(tasks,key=key)
