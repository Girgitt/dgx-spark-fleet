#!/usr/bin/env python3
import argparse, json, urllib.request
p=argparse.ArgumentParser()
p.add_argument('--base-url', required=True)
p.add_argument('--model', required=True)
p.add_argument('--api-key', default='EMPTY')
a=p.parse_args()
body={"model":a.model,"max_tokens":32,"messages":[{"role":"user","content":"Reply with exactly: OK"}]}
req=urllib.request.Request(a.base_url.rstrip('/')+'/chat/completions', data=json.dumps(body).encode(), headers={'Content-Type':'application/json','Authorization':'Bearer '+a.api_key})
with urllib.request.urlopen(req, timeout=60) as r:
    obj=json.load(r)
print(json.dumps(obj, indent=2)[:8000])
