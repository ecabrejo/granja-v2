import json
from pathlib import Path
cfg = json.loads(Path('/root/granja-v2/workers/worker_01/config.json').read_text())
if 'target_wallets' in cfg:
    w = cfg['target_wallets']
    print(f"{len(w)} wallet(s) | {w[0][:16]}... | {cfg.get('market','?')}")
else:
    print(f"{cfg.get('target_wallet','?')[:16]}... | {cfg.get('market','?')}")
