import boto3, os
from botocore.exceptions import ClientError
from dotenv import load_dotenv
load_dotenv()
R = os.environ["AWS_REGION"]
def t(label, fn):
    try:
        fn(); print(f"  OK      {label}")
    except ClientError as e:
        print(f"  DENIED  {label}  ({e.response['Error']['Code']})")
    except Exception as e:
        print(f"  ERR     {label}  {str(e)[:60]}")
t("ec2:DescribeInstances", lambda: boto3.client("ec2", region_name=R).describe_instances(MaxResults=5))
t("lambda:ListFunctions",  lambda: boto3.client("lambda", region_name=R).list_functions(MaxItems=1))
t("s3:ListBuckets",        lambda: boto3.client("s3").list_buckets())
t("apprunner:ListServices",lambda: boto3.client("apprunner", region_name=R).list_services(MaxResults=1))
t("ecr:DescribeRepos",     lambda: boto3.client("ecr", region_name=R).describe_repositories(maxResults=1))
t("iam:ListRoles",         lambda: boto3.client("iam").list_roles(MaxItems=1))
