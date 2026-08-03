import aws_cdk as cdk
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct


class OrdersStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        fn = _lambda.Function(
            self, "Worker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda"),
        )

        table = dynamodb.Table(
            self, "Orders",
            partition_key=dynamodb.Attribute(name="orderId", type=dynamodb.AttributeType.STRING),
        )

        table.grant_read_write_data(fn)
