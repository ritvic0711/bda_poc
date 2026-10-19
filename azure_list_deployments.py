#!/usr/bin/env python3
"""
List Azure OpenAI deployments + model versions using ONLY the
endpoint URL and an API key (data-plane). This does NOT return
retirement/expiration dates — the data plane doesn't expose them.
Pair the version output with the official retirement table.

pip install requests
"""

import requests

ENDPOINT = "https://YOUR-RESOURCE.openai.azure.com"   # your model URL root
API_KEY = "YOUR-SUBSCRIPTION-KEY"
API_VERSION = "2024-10-21"  # data-plane version

def main():
    url = f"{ENDPOINT}/openai/deployments"
    headers = {"api-key": API_KEY}
    params = {"api-version": API_VERSION}

    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json().get("data", [])

    print(f"{'DEPLOYMENT':<30} {'MODEL':<25} {'VERSION':<15}")
    print("-" * 70)
    for d in data:
        print(f"{d.get('id',''):<30} "
              f"{d.get('model',''):<25} "
              f"{d.get('model_version',''):<15}")

if __name__ == "__main__":
    main()
