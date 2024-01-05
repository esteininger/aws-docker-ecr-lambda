import boto3
import docker
import os
import base64
from botocore.exceptions import ClientError
import time


nux_aws_creds = {
    "s3_bucket": "",
    "aws_access_key": "",
    "aws_secret_key": "",
    "region": "",
    "role": ""
}


class LambdaContainerDeployer:
    def __init__(self,
                 function_name,
                 code,
                 requirements,
                 version="latest",  # Add version parameter
                 timeout=60,
                 memory_size=1024
                 ):
        self.session = boto3.Session(
            aws_access_key_id=nux_aws_creds['aws_access_key'],
            aws_secret_access_key=nux_aws_creds['aws_secret_key'],
            region_name=nux_aws_creds['region']
        )
        self.version = version  # Store the version
        self.role = nux_aws_creds['role']
        self.timout = timeout
        self.memory_size = memory_size
        self.function_name = function_name
        self.code = code
        self.requirements = requirements
        self.client = self.session.client('lambda')
        self.docker_client = docker.from_env()

    def _create_dockerfile(self):
        dockerfile = "FROM public.ecr.aws/lambda/python:3.8\n"
        dockerfile += "COPY . ${LAMBDA_TASK_ROOT}\n"
        dockerfile += "RUN pip install -r requirements.txt\n"
        dockerfile += f"CMD [\"{self.function_name}.handler\"]\n"
        return dockerfile

    def _write_files(self):
        os.makedirs(f'./{self.function_name}', exist_ok=True)
        with open(f'./{self.function_name}/Dockerfile', 'w') as f:
            f.write(self._create_dockerfile())
        with open(f'./{self.function_name}/{self.function_name}.py', 'w') as f:
            f.write(self.code)
        with open(f'./{self.function_name}/requirements.txt', 'w') as f:
            f.write('\n'.join(self.requirements))

    def _get_ecr_login_info(self):
        ecr_client = self.session.client('ecr')
        response = ecr_client.get_authorization_token()
        user, token = base64.b64decode(
            response['authorizationData'][0]['authorizationToken']).decode().split(':')
        registry = response['authorizationData'][0]['proxyEndpoint']
        return user, token, registry

    def _create_ecr_repository(self, repository_name):
        ecr_client = self.session.client('ecr')
        try:
            ecr_client.create_repository(repositoryName=repository_name)
            print(f"Repository {repository_name} created.")

        except ecr_client.exceptions.RepositoryAlreadyExistsException:
            print(f"Repository {repository_name} already exists.")

    def _build_and_push_image(self):
        self._write_files()
        user, token, registry = self._get_ecr_login_info()
        registry = registry.replace('https://', '')
        self.docker_client.login(
            username=user, password=token, registry=registry)

        # Modify the image name to include the version
        image_name = f"{registry}/{self.function_name}:{self.version}"
        image, build_log = self.docker_client.images.build(
            path=f"./{self.function_name}",
            tag=image_name,
            platform="linux/amd64"
        )

        self._create_ecr_repository(self.function_name)

        for line in self.docker_client.images.push(image_name, stream=True, decode=True):
            print(line)

        return image_name

    def _poll_lambda_status(self, callback, max_attempts=20, wait_interval=10):
        """
        Polls the status of the Lambda function and calls the callback when done.
        :param callback: Callback function to be called when Lambda is ready.
        :param max_attempts: Maximum number of status check attempts.
        :param wait_interval: Time to wait between attempts in seconds.
        """
        for _ in range(max_attempts):
            try:
                response = self.client.get_function(
                    FunctionName=self.function_name)
                if response['Configuration']['LastUpdateStatus'] == 'Successful':
                    callback()
                    return
            except ClientError as e:
                print(f"Waiting for Lambda function: {e}")

            time.sleep(wait_interval)

        print("Lambda function polling timed out.")

    def deploy(self, callback=None):
        image_uri = self._build_and_push_image()

        lambda_client = self.session.client('lambda')

        try:
            # Update the existing function
            response = lambda_client.update_function_code(
                FunctionName=self.function_name,
                ImageUri=image_uri
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                # If the function doesn't exist, create it
                response = lambda_client.create_function(
                    FunctionName=self.function_name,
                    Role=self.role,
                    Code={
                        'ImageUri': image_uri
                    },
                    PackageType='Image',
                    Timeout=30,  # Timeout in seconds
                    MemorySize=128  # Memory size in MB
                )
            else:
                raise

        print("Lambda function deployed:", response)
        if callback:
            self._poll_lambda_status(callback)


# Example usage
function_name = "my_lambda_function"
code = """
def handler(event, context):
    return {'statusCode': 200, 'body': 'dddd its Lambda!'}
"""
requirements = []
version = "v1.0"  # Specify the version

# Example usage


def lambda_deployed_callback():
    print("Lambda function is ready.")


deployer = LambdaContainerDeployer(function_name, code, requirements, version)
deployer.deploy(callback=lambda_deployed_callback)
