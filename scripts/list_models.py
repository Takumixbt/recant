"""List the Anthropic models and Claude inference profiles this account can see."""
import os, boto3
from dotenv import load_dotenv

load_dotenv()
region = os.environ["AWS_REGION"]
b = boto3.client("bedrock", region_name=region)

an = [m for m in b.list_foundation_models()["modelSummaries"]
      if m["modelId"].startswith("anthropic.")]
print(f"--- {len(an)} Anthropic foundation models in {region} ---")
for m in an:
    print(f"  {m['modelId']:<58} {','.join(m.get('inferenceTypesSupported', []))}")

print("\n--- Claude inference profiles (cross-region 'us.' ids) ---")
try:
    for x in b.list_inference_profiles().get("inferenceProfileSummaries", []):
        pid = x["inferenceProfileId"]
        if "claude" in pid.lower():
            print(f"  {pid:<58} {x.get('status', '')}")
except Exception as e:
    print("  list_inference_profiles failed:", str(e)[:150])
