"""Produce two original audition candidates without approving either voice."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from astro_tts.engine import TTSEngine


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    args=parser.parse_args()
    engine=TTSEngine(args.runtime)
    records=[]
    try:
        for profile_id in ('es-narrador','en-storyteller'):
            print(json.dumps({'stage':'starting','profile_id':profile_id}),flush=True)
            def progress(message,fraction):
                print(json.dumps({'profile_id':profile_id,'message':message,'fraction':fraction}),flush=True)
            result=engine.run_job('design',{'profile_id':profile_id,'seed':42,'master':True},progress)
            records.append(result)
            print(json.dumps({'stage':'sample_ready','profile_id':profile_id,
                              'audio_path':result['audio_path'],
                              'duration_seconds':result['duration_seconds'],
                              'metrics':result['metrics'],'warnings':result['warnings']}),flush=True)
    finally:
        engine.unload()
        (args.runtime/'first-samples.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    main()
