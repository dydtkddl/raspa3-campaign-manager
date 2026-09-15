"""Read six CIF cell scalars and compute perpendicular-face replication.

This is not a CIF atom/symmetry parser and does not compute PLD/LCD.
The legacy diagonal rule is exposed only as a comparison, never a fallback.
"""
from __future__ import annotations
import math
import re
import shlex
from .config import ConfigError

KEYS = ('_cell_length_a','_cell_length_b','_cell_length_c',
        '_cell_angle_alpha','_cell_angle_beta','_cell_angle_gamma')
NUM = re.compile(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?)(?:\(\d+\))?$')


def cell_from_cif(raw: bytes) -> dict:
    text=raw.decode('utf-8-sig');values={};blocks=[];pending=None;in_text=False
    for line in text.splitlines():
        if line.startswith(';'):
            in_text=not in_text;continue
        if in_text:continue
        first_raw=line.strip().split(maxsplit=1)[0].lower() if line.strip() else ''
        if pending is None and first_raw not in KEYS and not first_raw.startswith('data_'):
            continue
        tokens=shlex.split(line,comments=True,posix=True)
        if not tokens:continue
        first=tokens[0].lower()
        if first.startswith('data_'):
            blocks.append(first)
        if len(blocks)>1:
            raise ConfigError('UnitCells reader requires one CIF data block; select a single structure explicitly')
        if pending:
            key=pending;pending=None
            if len(tokens)!=1 or first.startswith('_'):
                raise ConfigError('Missing scalar CIF cell value: '+key)
            val=tokens[0]
        elif first in KEYS:
            key=first
            if key in values:raise ConfigError('Duplicate CIF cell scalar: '+key)
            if len(tokens)==1:
                pending=key;continue
            if len(tokens)!=2:raise ConfigError('Ambiguous CIF cell scalar: '+key)
            val=tokens[1]
        else:continue
        if not NUM.fullmatch(val):raise ConfigError('Invalid CIF cell number: '+key)
        values[key]=float(NUM.fullmatch(val).group(1).replace('D','e').replace('d','e'))
    if pending or set(values)!=set(KEYS):raise ConfigError('Missing CIF lengths or angles for UnitCells')
    a,b,c,alpha,beta,gamma=[values[k] for k in KEYS]
    if any(not math.isfinite(v) or v<=0 for v in (a,b,c)) or any(not math.isfinite(v) or not 0<v<180 for v in (alpha,beta,gamma)):
        raise ConfigError('CIF cell lengths/angles out of range')
    ca,cb,cg=map(lambda x: math.cos(math.radians(x)),(alpha,beta,gamma))
    sa,sb,sg=map(lambda x: math.sin(math.radians(x)),(alpha,beta,gamma))
    determinant=1-ca*ca-cb*cb-cg*cg+2*ca*cb*cg
    if determinant<=1e-14 or min(sa,sb,sg)<=1e-10:raise ConfigError('Degenerate/nonphysical CIF cell')
    factor=math.sqrt(determinant)
    volume=a*b*c*factor
    heights=[a*factor/sa,b*factor/sb,c*factor/sg]
    diagonals=[a,b*sg,c*factor/sg]
    return {'lengths_A':[a,b,c],'angles_degree':[alpha,beta,gamma],
            'volume_A3':volume,'face_heights_A':heights,'legacy_diagonal_A':diagonals}


def replication(raw: bytes, cutoff_A: float) -> dict:
    if isinstance(cutoff_A,bool) or not isinstance(cutoff_A,(int,float)) or not math.isfinite(cutoff_A) or cutoff_A<=0:
        raise ConfigError('unitcells.cutoff_A must be an explicit positive finite Angstrom value')
    cell=cell_from_cif(raw);repeat=[]
    for h in cell['face_heights_A']:
        n=max(1,math.ceil(2*cutoff_A/h))
        if n*h<2*cutoff_A:n+=1
        repeat.append(n)
    if max(repeat)>10000 or math.prod(repeat)>10000000:
        raise ConfigError('UnitCells replication exceeds protective cap; inspect cell/cutoff')
    legacy=[max(1,math.ceil(2*cutoff_A/h)) for h in cell['legacy_diagonal_A']]
    return {'mode':'auto_face_heights','cutoff_A':cutoff_A,**cell,'number_of_unit_cells':repeat,
            'replicated_face_heights_A':[h*n for h,n in zip(cell['face_heights_A'],repeat)],
            'legacy_diagonal_repeats':legacy,'differs_from_legacy_diagonal':legacy!=repeat,
            'rule':'ceil(2*cutoff_A/perpendicular_face_height_A); not ceil(2*cutoff/cell_vector_diagonal)'}
