"""Fetch only the reviewed public model revisions; inference uses local folders."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def download(runtime: Path, lock: Path) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ['HF_HOME'] = str(runtime/'cache/huggingface')
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    from huggingface_hub import snapshot_download
    models = json.loads(lock.read_text(encoding='utf-8'))
    for name, spec in models.items():
        destination = runtime/'models'/spec['directory']
        destination.mkdir(parents=True, exist_ok=True)
        print(json.dumps({'stage':'downloading','model':name,'revision':spec['revision']}),flush=True)
        snapshot_download(repo_id=spec['repo_id'], revision=spec['revision'],
                          local_dir=str(destination), token=False, max_workers=3,
                          allow_patterns=['*.json','*.txt','*.safetensors','README.md',
                                          'LICENSE','speech_tokenizer/**'])
        for required in ('config.json','model.safetensors','speech_tokenizer/config.json'):
            if not (destination/required).is_file():
                raise RuntimeError('Incomplete model snapshot: '+name+'/'+required)
        print(json.dumps({'stage':'downloaded','model':name}),flush=True)
    (runtime/'models/model-lock.json').write_text(json.dumps(models,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'models_ready':True,'runtime':str(runtime)}),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--lock', type=Path,
                        default=Path(__file__).resolve().parents[1]/'models.lock.json')
    args = parser.parse_args()
    download(args.runtime.resolve(),args.lock.resolve())
