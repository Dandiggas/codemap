import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export class OrdersStack extends cdk.Stack {
  constructor(scope: Construct, id: string) {
    super(scope, id);

    const fn = new lambda.Function(this, "Worker", {
      runtime: lambda.Runtime.NODEJS_18_X,
      handler: "index.handler",
      code: lambda.Code.fromAsset("lambda"),
    });

    const table = new dynamodb.Table(this, "Orders", {
      partitionKey: { name: "orderId", type: dynamodb.AttributeType.STRING },
    });

    const bucket = new s3.Bucket(this, "Uploads");

    const queue = new sqs.Queue(this, "DeadLetter");

    table.grantReadWriteData(fn);
    bucket.grantRead(fn);

    const execRole = new iam.Role(this, "ExecRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    });
    execRole.addToPolicy(new iam.PolicyStatement({
      actions: ["sqs:SendMessage"],
      resources: [queue.queueArn],
    }));

    const notifier = new lambda.Function(this, "Notifier", {
      runtime: lambda.Runtime.NODEJS_18_X,
      handler: "index.handler",
      code: lambda.Code.fromAsset("lambda2"),
      role: execRole,
    });
  }
}
