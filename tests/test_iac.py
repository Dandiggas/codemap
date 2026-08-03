import json, unittest, tempfile
from pathlib import Path
from lib.iac import scan_iac, parse_terraform_text, parse_yaml_lite, group_of
from lib.iac_cfn import (
    is_cfn_doc, is_serverless_doc, parse_cfn_doc, parse_serverless_doc,
)
from lib.iac_k8s import (
    is_compose_doc, parse_compose_doc, split_yaml_docs, parse_doc as parse_iac_k8s_doc,
)
from lib.iac_cdk import is_cdk_ts, is_cdk_py, parse_cdk_ts, parse_cdk_py

FIX = Path(__file__).parent / "fixtures" / "iacrepo"
FIX_MULTIENV = Path(__file__).parent / "fixtures" / "multienvrepo"

class TestScanIacTerraform(unittest.TestCase):
    def test_all_resources_with_lines(self):
        m = scan_iac(FIX)
        # the fixture now also carries a CFN/SAM template and a serverless.yml
        # (task 2); scope to main.tf's own resources, which are unaffected.
        by_id = {r["id"]: r for r in m["resources"] if r["source_file"] == "main.tf"}
        self.assertEqual(len(by_id), 6)
        # main.tf sits at the repo root, so its module dirpath is "."
        self.assertEqual(by_id["tf:.::aws_lambda_function.process_order"]["line"], 1)
        self.assertEqual(by_id["tf:.::aws_dynamodb_table.orders"]["line"], 8)
        self.assertEqual(by_id["tf:.::aws_s3_bucket.uploads"]["line"], 19)
        self.assertEqual(by_id["tf:.::aws_iam_role.lambda_exec"]["line"], 23)
        self.assertEqual(by_id["tf:.::aws_sqs_queue.dead_letters"]["line"], 59)
        self.assertEqual(by_id["tf:.::aws_iam_role.reader_role"]["line"], 63)
        for rid, expected_file in [(k, "main.tf") for k in by_id]:
            self.assertEqual(by_id[rid]["source_file"], expected_file)
        # the policy-document / attachment resources are not generic boxes
        self.assertNotIn("tf:.::aws_iam_role_policy.lambda_policy", by_id)
        self.assertNotIn("tf:.::aws_iam_role_policy_attachment.reader_dynamodb_ro", by_id)

    def test_role_has_both_grants(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"] if r["source_file"] == "main.tf"}
        self.assertEqual(len(roles), 2)
        role = roles["tf:.::aws_iam_role.lambda_exec"]
        # the id is module-qualified; the display name stays bare
        self.assertEqual(role["name"], "lambda_exec")
        self.assertEqual(role["source_file"], "main.tf")
        self.assertEqual(role["line"], 23)
        self.assertEqual(role["attached_to"], ["tf:.::aws_lambda_function.process_order"])
        grants_by_on = {g["on"]: g["actions"] for g in role["grants"]}
        self.assertEqual(len(role["grants"]), 2)
        self.assertEqual(
            sorted(grants_by_on["tf:.::aws_dynamodb_table.orders"]),
            ["dynamodb:GetItem", "dynamodb:PutItem"],
        )
        self.assertEqual(grants_by_on["tf:.::aws_s3_bucket.uploads"], ["s3:GetObject"])

    def test_lambda_connections_via_role(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"]
                 if c["src"] == "tf:.::aws_lambda_function.process_order"]
        self.assertEqual(len(conns), 2)
        by_dst = {c["dst"]: c for c in conns}

        dynamo = by_dst["tf:.::aws_dynamodb_table.orders"]
        self.assertEqual(dynamo["mechanism"], "via role")
        self.assertEqual(dynamo["role"], "lambda_exec")
        self.assertEqual(dynamo["evidence"], "main.tf:45")

        s3 = by_dst["tf:.::aws_s3_bucket.uploads"]
        self.assertEqual(s3["mechanism"], "via role")
        self.assertEqual(s3["role"], "lambda_exec")
        self.assertEqual(s3["evidence"], "main.tf:50")

    def test_managed_policy_attachment_grants(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        reader = roles["tf:.::aws_iam_role.reader_role"]
        self.assertEqual(reader["grants"], [{"actions": ["managed"], "on": "managed:AmazonDynamoDBReadOnlyAccess"}])
        # a managed policy has no in-repo target resource, so it produces no connection
        touching_reader = [c for c in m["connections"]
                            if c["src"] == "tf:.::aws_iam_role.reader_role"
                            or c["dst"] == "tf:.::aws_iam_role.reader_role"]
        self.assertEqual(touching_reader, [])

    def test_queue_has_no_connections(self):
        m = scan_iac(FIX)
        touching_queue = [c for c in m["connections"]
                           if c["src"] == "tf:.::aws_sqs_queue.dead_letters"
                           or c["dst"] == "tf:.::aws_sqs_queue.dead_letters"]
        self.assertEqual(touching_queue, [])

    def test_no_tf_files_returns_empty_model(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "app.py").write_text("print(1)\n")
            m = scan_iac(Path(td))
            self.assertEqual(m, {"resources": [], "roles": [], "connections": [], "deployed": {}})

    def test_deployed_always_empty_dict(self):
        m = scan_iac(FIX)
        self.assertEqual(m["deployed"], {})


class TestParseTerraformText(unittest.TestCase):
    def test_direct_reference_produces_references_connection(self):
        text = (
            'resource "aws_s3_bucket" "logs" {\n'
            '  bucket = "logs"\n'
            '}\n\n'
            'resource "aws_lambda_function" "shipper" {\n'
            '  function_name = "shipper"\n'
            '  environment {\n'
            '    variables = {\n'
            '      BUCKET = aws_s3_bucket.logs.bucket\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
        m = parse_terraform_text(text, "main.tf")
        self.assertEqual(len(m["connections"]), 1)
        c = m["connections"][0]
        self.assertEqual(c["src"], "tf:.::aws_lambda_function.shipper")
        self.assertEqual(c["dst"], "tf:.::aws_s3_bucket.logs")
        self.assertEqual(c["mechanism"], "references")
        self.assertIsNone(c["role"])
        self.assertEqual(c["evidence"], "main.tf:9")

    def test_heredoc_policy_document_grants(self):
        text = (
            'resource "aws_dynamodb_table" "orders" {\n'
            '  name = "orders"\n'
            '}\n\n'
            'resource "aws_iam_role" "worker" {\n'
            '  name = "worker-role"\n'
            '}\n\n'
            'resource "aws_iam_role_policy" "worker_policy" {\n'
            '  name = "worker-policy"\n'
            '  role = aws_iam_role.worker.id\n\n'
            '  policy = <<POLICY\n'
            '{\n'
            '  "Version": "2012-10-17",\n'
            '  "Statement": [\n'
            '    {\n'
            '      "Effect": "Allow",\n'
            '      "Action": ["dynamodb:Query"],\n'
            '      "Resource": "${aws_dynamodb_table.orders.arn}"\n'
            '    }\n'
            '  ]\n'
            '}\n'
            'POLICY\n'
            '}\n\n'
            'resource "aws_ecs_task_definition" "worker_task" {\n'
            '  family = "worker"\n'
            '  execution_role_arn = aws_iam_role.worker.arn\n'
            '}\n'
        )
        m = parse_terraform_text(text, "main.tf")
        role = m["roles"][0]
        self.assertEqual(role["grants"], [{"actions": ["dynamodb:Query"], "on": "tf:.::aws_dynamodb_table.orders"}])
        self.assertEqual(m["connections"], [{
            "src": "tf:.::aws_ecs_task_definition.worker_task",
            "dst": "tf:.::aws_dynamodb_table.orders",
            "mechanism": "via role",
            "role": "worker",
            "evidence": "main.tf:17",
        }])

    def test_heredoc_unbalanced_brace_does_not_swallow_next_block(self):
        # An unmatched "{" inside a user_data heredoc must not leak into the
        # enclosing resource block's depth count: without heredoc-skipping,
        # aws_instance.web's body_end overruns past its real closing brace,
        # swallows aws_lambda_function.worker whole, and its `role = ...`
        # line gets misread as belonging to aws_instance.web -- falsely
        # attaching it to worker_role.
        text = (
            'resource "aws_iam_role" "worker_role" {\n'
            '  name = "worker-role"\n'
            '}\n\n'
            'resource "aws_instance" "web" {\n'
            '  ami           = "ami-12345"\n'
            '  instance_type = "t3.micro"\n'
            '  user_data = <<-EOF\n'
            '    #!/bin/bash\n'
            '    echo start { of something unbalanced\n'
            '  EOF\n'
            '}\n\n'
            'resource "aws_lambda_function" "worker" {\n'
            '  function_name = "worker"\n'
            '  role          = aws_iam_role.worker_role.arn\n'
            '}\n'
        )
        m = parse_terraform_text(text, "main.tf")
        roles = {r["id"]: r for r in m["roles"]}
        self.assertEqual(roles["tf:.::aws_iam_role.worker_role"]["attached_to"],
                         ["tf:.::aws_lambda_function.worker"])
        # and aws_instance.web's own block must still be found as its own resource
        self.assertIn("tf:.::aws_instance.web", {r["id"] for r in m["resources"]})

    def test_resource_list_truncates_to_first_element_known_limitation(self):
        # Documents today's lossy behaviour for a list-valued Resource
        # (Resource = [a.arn, b.arn]): only the first element is resolved.
        # Ledgered punt, not fixed here -- this pins the behaviour so a
        # future change can't silently make it worse (e.g. crash, or
        # silently drop the grant instead of keeping the first element).
        text = (
            'resource "aws_dynamodb_table" "orders" {\n'
            '  name = "orders"\n'
            '}\n'
            'resource "aws_dynamodb_table" "archive" {\n'
            '  name = "archive"\n'
            '}\n'
            'resource "aws_iam_role" "worker" {\n'
            '  name = "worker-role"\n'
            '}\n'
            'resource "aws_iam_role_policy" "worker_policy" {\n'
            '  role = aws_iam_role.worker.id\n'
            '  policy = jsonencode({\n'
            '    Statement = [\n'
            '      {\n'
            '        Effect   = "Allow"\n'
            '        Action   = ["dynamodb:Query"]\n'
            '        Resource = [aws_dynamodb_table.orders.arn, aws_dynamodb_table.archive.arn]\n'
            '      }\n'
            '    ]\n'
            '  })\n'
            '}\n'
        )
        m = parse_terraform_text(text, "main.tf")
        role = m["roles"][0]
        self.assertEqual(role["grants"], [{"actions": ["dynamodb:Query"], "on": "tf:.::aws_dynamodb_table.orders"}])

    def test_no_resource_blocks_returns_empty(self):
        m = parse_terraform_text("# just a comment\nvariable \"x\" {}\n", "vars.tf")
        self.assertEqual(m, {"resources": [], "roles": [], "connections": [], "deployed": {}})


class TestYamlLite(unittest.TestCase):
    def test_flat_mapping_and_types(self):
        r = parse_yaml_lite("name: myapp\nversion: 2\ndebug: true\nmissing: null\n")
        self.assertEqual(r, {"name": "myapp", "version": 2, "debug": True, "missing": None})

    def test_nested_mapping(self):
        r = parse_yaml_lite("db:\n  host: localhost\n  port: 5432\n")
        self.assertEqual(r, {"db": {"host": "localhost", "port": 5432}})

    def test_list_of_scalars(self):
        r = parse_yaml_lite("colors:\n  - red\n  - green\n  - blue\n")
        self.assertEqual(r, {"colors": ["red", "green", "blue"]})

    def test_list_of_mappings_dash_inline(self):
        r = parse_yaml_lite("items:\n  - name: a\n    value: 1\n  - name: b\n    value: 2\n")
        self.assertEqual(r, {"items": [{"name": "a", "value": 1}, {"name": "b", "value": 2}]})

    def test_compose_style_services(self):
        text = (
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "    depends_on:\n"
            "      - db\n"
            "  db:\n"
            "    image: postgres\n"
        )
        r = parse_yaml_lite(text)
        self.assertEqual(r, {
            "services": {
                "web": {"image": "nginx", "depends_on": ["db"]},
                "db": {"image": "postgres"},
            }
        })

    def test_k8s_style_nested_list_of_maps(self):
        text = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "spec:\n"
            "  containers:\n"
            "    - name: web\n"
            "      image: nginx:latest\n"
            "      env:\n"
            "        - name: DEBUG\n"
            "          value: \"false\"\n"
        )
        r = parse_yaml_lite(text)
        self.assertEqual(r["apiVersion"], "apps/v1")
        self.assertEqual(r["kind"], "Deployment")
        containers = r["spec"]["containers"]
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0]["name"], "web")
        self.assertEqual(containers[0]["env"], [{"name": "DEBUG", "value": "false"}])

    def test_literal_block_scalar_preserves_newlines(self):
        text = "script: |\n  echo hello\n  echo world\nother: fine\n"
        r = parse_yaml_lite(text)
        self.assertEqual(r, {"script": "echo hello\necho world", "other": "fine"})

    def test_folded_block_scalar_joins_with_spaces(self):
        text = "desc: >\n  this is\n  folded text\n"
        r = parse_yaml_lite(text)
        self.assertEqual(r, {"desc": "this is folded text"})

    def test_comments_and_quoted_strings(self):
        text = "# leading comment\nkey: value # trailing comment\nquoted: \"hi there\"\n"
        r = parse_yaml_lite(text)
        self.assertEqual(r, {"key": "value", "quoted": "hi there"})

    def test_top_level_list(self):
        r = parse_yaml_lite("- a\n- b\n- c\n")
        self.assertEqual(r, ["a", "b", "c"])

    def test_empty_document_returns_empty_dict(self):
        self.assertEqual(parse_yaml_lite(""), {})
        self.assertEqual(parse_yaml_lite("\n\n"), {})


class TestScanIacCfnSam(unittest.TestCase):
    """template.yaml: a SAM function with an explicit IAM role (inline
    policy) + an SQS event source. Ids are prefixed "cfn:<file>::<LogicalId>"
    -- see the id-scheme note at the top of lib/iac_cfn.py."""

    def test_resources_include_table_queue_function_and_role(self):
        m = scan_iac(FIX)
        cfn = {r["id"]: r for r in m["resources"] if r["id"].startswith("cfn:")}
        # table, queue, function, role, + the ECS role/task-def pair
        # appended for task-2 coverage (see TestScanIacCfnEcsTaskDefinition)
        self.assertEqual(len(cfn), 6)
        self.assertEqual(cfn["cfn:template.yaml::OrdersTable"]["rtype"], "AWS::DynamoDB::Table")
        self.assertEqual(cfn["cfn:template.yaml::OrdersTable"]["line"], 4)
        self.assertEqual(cfn["cfn:template.yaml::OrdersQueue"]["rtype"], "AWS::SQS::Queue")
        self.assertEqual(cfn["cfn:template.yaml::OrdersQueue"]["line"], 16)
        self.assertEqual(cfn["cfn:template.yaml::ProcessOrderFunction"]["rtype"], "AWS::Serverless::Function")
        self.assertEqual(cfn["cfn:template.yaml::ProcessOrderFunction"]["line"], 21)
        # the role is a resource box too (mirrors aws_iam_role in the tf parser)
        self.assertEqual(cfn["cfn:template.yaml::ProcessOrderRole"]["rtype"], "AWS::IAM::Role")
        self.assertEqual(cfn["cfn:template.yaml::ProcessOrderRole"]["line"], 34)
        for r in cfn.values():
            self.assertEqual(r["source_file"], "template.yaml")

    def test_explicit_role_has_inline_grants(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        role = roles["cfn:template.yaml::ProcessOrderRole"]
        self.assertEqual(role["name"], "ProcessOrderRole")
        # also reused as WorkerTaskDefinition's TaskRoleArn (task-2 ECS
        # coverage) -- see TestScanIacCfnEcsTaskDefinition
        self.assertEqual(role["attached_to"], [
            "cfn:template.yaml::ProcessOrderFunction",
            "cfn:template.yaml::WorkerTaskDefinition",
        ])
        grants_by_on = {g["on"]: g["actions"] for g in role["grants"]}
        self.assertEqual(len(role["grants"]), 2)
        self.assertEqual(
            sorted(grants_by_on["cfn:template.yaml::OrdersTable"]),
            ["dynamodb:GetItem", "dynamodb:PutItem"],
        )
        self.assertEqual(grants_by_on["cfn:template.yaml::OrdersQueue"], ["sqs:ReceiveMessage"])

    def test_via_role_connections_carry_evidence(self):
        m = scan_iac(FIX)
        conns = {(c["src"], c["dst"]): c for c in m["connections"]
                 if c["mechanism"] == "via role" and c["src"] == "cfn:template.yaml::ProcessOrderFunction"}
        self.assertEqual(len(conns), 2)
        table = conns[("cfn:template.yaml::ProcessOrderFunction", "cfn:template.yaml::OrdersTable")]
        self.assertEqual(table["role"], "ProcessOrderRole")
        self.assertEqual(table["evidence"], "template.yaml:48")
        queue = conns[("cfn:template.yaml::ProcessOrderFunction", "cfn:template.yaml::OrdersQueue")]
        self.assertEqual(queue["role"], "ProcessOrderRole")
        self.assertEqual(queue["evidence"], "template.yaml:53")

    def test_sqs_event_source_connection(self):
        m = scan_iac(FIX)
        events = [c for c in m["connections"] if c["mechanism"] == "event source"]
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["src"], "cfn:template.yaml::OrdersQueue")
        self.assertEqual(ev["dst"], "cfn:template.yaml::ProcessOrderFunction")
        self.assertIsNone(ev["role"])
        self.assertEqual(ev["evidence"], "template.yaml:29")


class TestScanIacCfnEcsTaskDefinition(unittest.TestCase):
    """template.yaml also carries an AWS::ECS::TaskDefinition with two
    independent role attributes: ExecutionRoleArn -> a new EcsExecutionRole,
    TaskRoleArn -> the existing ProcessOrderRole (reused). Appended after the
    task-1/task-2 fixture content -- lines 1-55 (and their existing pins)
    are untouched; see test_existing_pins_untouched_by_append below."""

    def test_new_resources_present(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"] if r["source_file"] == "template.yaml"}
        self.assertEqual(by_id["cfn:template.yaml::EcsExecutionRole"]["rtype"], "AWS::IAM::Role")
        self.assertEqual(by_id["cfn:template.yaml::EcsExecutionRole"]["line"], 57)
        self.assertEqual(by_id["cfn:template.yaml::WorkerTaskDefinition"]["rtype"], "AWS::ECS::TaskDefinition")
        self.assertEqual(by_id["cfn:template.yaml::WorkerTaskDefinition"]["line"], 75)

    def test_execution_role_arn_attachment_via_role_connection(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        exec_role = roles["cfn:template.yaml::EcsExecutionRole"]
        self.assertEqual(exec_role["grants"], [{"actions": ["sqs:SendMessage"], "on": "cfn:template.yaml::OrdersQueue"}])
        self.assertIn("cfn:template.yaml::WorkerTaskDefinition", exec_role["attached_to"])
        conns = [c for c in m["connections"]
                 if c["src"] == "cfn:template.yaml::WorkerTaskDefinition" and c["role"] == "EcsExecutionRole"]
        self.assertEqual(conns, [{
            "src": "cfn:template.yaml::WorkerTaskDefinition", "dst": "cfn:template.yaml::OrdersQueue",
            "mechanism": "via role", "role": "EcsExecutionRole", "evidence": "template.yaml:71",
        }])

    def test_task_role_arn_attachment_via_role_connection(self):
        # TaskRoleArn points at the *existing* ProcessOrderRole (reused from
        # the SAM function's Role:) -- an independent attachment path from
        # ExecutionRoleArn's, exercised on the same TaskDefinition resource.
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        process_order_role = roles["cfn:template.yaml::ProcessOrderRole"]
        self.assertIn("cfn:template.yaml::WorkerTaskDefinition", process_order_role["attached_to"])
        conns = {(c["dst"]): c for c in m["connections"]
                 if c["src"] == "cfn:template.yaml::WorkerTaskDefinition" and c["role"] == "ProcessOrderRole"}
        self.assertEqual(len(conns), 2)
        self.assertEqual(conns["cfn:template.yaml::OrdersTable"]["evidence"], "template.yaml:48")
        self.assertEqual(conns["cfn:template.yaml::OrdersQueue"]["evidence"], "template.yaml:53")

    def test_execution_and_task_role_connections_are_independent(self):
        # both role attributes on the same resource produce their own
        # distinct "via role" edges, none of them cross-attributed
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["src"] == "cfn:template.yaml::WorkerTaskDefinition"]
        self.assertEqual(len(conns), 3)
        roles_seen = {c["role"] for c in conns}
        self.assertEqual(roles_seen, {"EcsExecutionRole", "ProcessOrderRole"})

    def test_existing_pins_untouched_by_append(self):
        # the append-only fixture edit must not have shifted any task-1/
        # task-2 line pins
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"] if r["source_file"] == "template.yaml"}
        self.assertEqual(by_id["cfn:template.yaml::OrdersTable"]["line"], 4)
        self.assertEqual(by_id["cfn:template.yaml::OrdersQueue"]["line"], 16)
        self.assertEqual(by_id["cfn:template.yaml::ProcessOrderFunction"]["line"], 21)
        self.assertEqual(by_id["cfn:template.yaml::ProcessOrderRole"]["line"], 34)


class TestScanIacServerless(unittest.TestCase):
    """serverless.yml: one function, iamRoleStatements granting s3:PutObject
    on a literal bucket ARN (no embedded resources.Resources block, so the
    ARN has no internal resource to resolve to and stays a literal)."""

    def test_function_resource(self):
        m = scan_iac(FIX)
        sls = {r["id"]: r for r in m["resources"] if r["id"].startswith("sls:")}
        self.assertEqual(len(sls), 1)
        fn = sls["sls:serverless.yml::uploadReceipt"]
        self.assertEqual(fn["rtype"], "serverless function")
        self.assertEqual(fn["name"], "uploadReceipt")
        self.assertEqual(fn["source_file"], "serverless.yml")
        self.assertEqual(fn["line"], 13)

    def test_provider_implicit_role_grant(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        role = roles["sls:serverless.yml::provider"]
        self.assertEqual(role["name"], "serverless (provider)")
        self.assertEqual(role["attached_to"], ["sls:serverless.yml::uploadReceipt"])
        self.assertEqual(role["grants"], [{
            "actions": ["s3:PutObject"],
            "on": "arn:aws:s3:::orders-uploads-bucket/*",
        }])

    def test_literal_arn_grant_produces_no_connection(self):
        m = scan_iac(FIX)
        touching = [c for c in m["connections"]
                    if c["src"] == "sls:serverless.yml::provider"
                    or c["dst"] == "sls:serverless.yml::provider"
                    or c["src"] == "sls:serverless.yml::uploadReceipt"
                    or c["dst"] == "sls:serverless.yml::uploadReceipt"]
        self.assertEqual(touching, [])


class TestScanIacMerge(unittest.TestCase):
    """scan_iac merges Terraform + CFN/SAM + serverless.yml + compose + k8s
    results with no id collisions: tf ids are "tf:<dirpath>::<type>.<name>"
    (dirpath repo-relative, "." at the root -- see lib/iac.py), cfn ids are
    "cfn:<file>::<LogicalId>", serverless ids are "sls:<file>::<name>",
    compose ids are "compose:<file>::<service>", k8s ids are
    "k8s:<file>::<kind>/<name>" (or "k8s:<file>::sa/<name>" for a
    ServiceAccount's role identity -- see lib/iac_k8s.py)."""

    def test_all_three_sources_present_with_unique_ids(self):
        # An IAM role legitimately appears in both `resources` (as a box) and
        # `roles` (with its grants) under the *same* id -- that's by design,
        # matching the Terraform parser's aws_iam_role convention, not a
        # collision. What must never happen is two *different* underlying
        # resources sharing one id, so check uniqueness within each list.
        m = scan_iac(FIX)
        resource_ids = [r["id"] for r in m["resources"]]
        role_ids = [r["id"] for r in m["roles"]]
        self.assertEqual(len(resource_ids), len(set(resource_ids)), "duplicate id within resources")
        self.assertEqual(len(role_ids), len(set(role_ids)), "duplicate id within roles")
        all_ids = resource_ids + role_ids
        self.assertTrue(any(i.startswith("tf:.::aws_") for i in all_ids))
        self.assertTrue(any(i.startswith("cfn:template.yaml::") for i in all_ids))
        self.assertTrue(any(i.startswith("sls:serverless.yml::") for i in all_ids))
        self.assertTrue(any(i.startswith("compose:docker-compose.yml::") for i in all_ids))
        self.assertTrue(any(i.startswith("k8s:k8s/app.yaml::") for i in all_ids))
        self.assertTrue(any(i.startswith("cdk:cdk/stack.ts::") for i in all_ids))
        self.assertTrue(any(i.startswith("cdk:cdk/stack.py::") for i in all_ids))

    def test_resource_and_role_and_connection_counts(self):
        m = scan_iac(FIX)
        # 6 terraform + 6 cfn (table, queue, function, role, ecs role, ecs
        # task def) + 1 serverless function + 2 compose services (web,
        # redis) + 4 k8s (Deployment, Service, ServiceAccount, ConfigMap --
        # Role/RoleBinding are wiring, not boxes, same as a CFN policy doc)
        # + 6 cdk/stack.ts (Worker, Orders, Uploads, DeadLetter, ExecRole,
        # Notifier) + 2 cdk/stack.py (Worker, Orders)
        self.assertEqual(len(m["resources"]), 27)
        # 2 terraform + 2 cfn explicit roles (ProcessOrderRole, EcsExecutionRole)
        # + 1 serverless implicit provider role + 1 k8s ServiceAccount role
        # identity (sa/app-sa) + 2 cdk/stack.ts (explicit ExecRole, implicit
        # role/Worker) + 1 cdk/stack.py (implicit role/Worker)
        self.assertEqual(len(m["roles"]), 9)
        # 2 terraform via-role + 1 cfn event source + 2 cfn via-role
        # (ProcessOrderRole -> ProcessOrderFunction, table+queue) + 2 cfn
        # via-role (ProcessOrderRole reused -> WorkerTaskDefinition's
        # TaskRoleArn, table+queue) + 1 cfn via-role (EcsExecutionRole ->
        # WorkerTaskDefinition's ExecutionRoleArn, queue) + 4 compose
        # (depends on, links, network dns, shared volume, all web->redis)
        # + 3 k8s (mounts, runs as, selects) + 3 cdk/stack.ts (2 grant
        # connections + 1 via-role Notifier->DeadLetter) + 1 cdk/stack.py
        # (1 grant connection) = 2+1+2+2+1+4+3+3+1
        self.assertEqual(len(m["connections"]), 19)

    def test_main_tf_untouched(self):
        # main.tf's own resources/roles/connections are unaffected by the new parsers
        m = scan_iac(FIX)
        tf_resources = [r for r in m["resources"] if r["source_file"] == "main.tf"]
        self.assertEqual(len(tf_resources), 6)


class TestIsCfnDoc(unittest.TestCase):
    def test_true_for_resources_with_typed_entries(self):
        self.assertTrue(is_cfn_doc({"Resources": {"X": {"Type": "AWS::S3::Bucket"}}}))

    def test_false_without_resources(self):
        self.assertFalse(is_cfn_doc({"functions": {}}))

    def test_false_for_unrelated_resources_key(self):
        # a "Resources" key whose entries don't look like CFN resources
        # (no Type) must not false-positive
        self.assertFalse(is_cfn_doc({"Resources": {"X": {"name": "not-cfn"}}}))

    def test_false_for_non_dict(self):
        self.assertFalse(is_cfn_doc(["a", "b"]))
        self.assertFalse(is_cfn_doc(None))


class TestIsServerlessDoc(unittest.TestCase):
    def test_true_for_service_and_functions(self):
        self.assertTrue(is_serverless_doc({"service": "x", "functions": {"a": {}}}))

    def test_false_without_service(self):
        self.assertFalse(is_serverless_doc({"functions": {"a": {}}}))

    def test_false_with_empty_functions(self):
        self.assertFalse(is_serverless_doc({"service": "x", "functions": {}}))


class TestParseCfnDocDirect(unittest.TestCase):
    def test_json_template_parses_same_as_yaml(self):
        # *.json CFN templates use the same long-form Ref/GetAtt intrinsics
        text = json.dumps({
            "Resources": {
                "MyTable": {"Type": "AWS::DynamoDB::Table", "Properties": {"TableName": "t"}},
                "MyRole": {
                    "Type": "AWS::IAM::Role",
                    "Properties": {
                        "Policies": [{"PolicyName": "p", "PolicyDocument": {"Statement": [
                            {"Effect": "Allow", "Action": ["dynamodb:GetItem"],
                             "Resource": {"Fn::GetAtt": ["MyTable", "Arn"]}}
                        ]}}]
                    },
                },
                "MyFn": {"Type": "AWS::Lambda::Function",
                         "Properties": {"Role": {"Fn::GetAtt": ["MyRole", "Arn"]}}},
            }
        }, indent=2)
        parsed = json.loads(text)
        self.assertTrue(is_cfn_doc(parsed))
        m = parse_cfn_doc(parsed, text, "t.json")
        roles = {r["id"]: r for r in m["roles"]}
        role = roles["cfn:t.json::MyRole"]
        self.assertEqual(role["grants"], [{"actions": ["dynamodb:GetItem"], "on": "cfn:t.json::MyTable"}])
        self.assertEqual(role["attached_to"], ["cfn:t.json::MyFn"])
        self.assertEqual(m["connections"], [{
            "src": "cfn:t.json::MyFn", "dst": "cfn:t.json::MyTable",
            "mechanism": "via role", "role": "MyRole", "evidence": "t.json:9",
        }])

    def test_managed_policy_arn_literal_grant(self):
        text = (
            "Resources:\n"
            "  ReaderRole:\n"
            "    Type: AWS::IAM::Role\n"
            "    Properties:\n"
            "      ManagedPolicyArns:\n"
            "        - arn:aws:iam::aws:policy/AmazonDynamoDBReadOnlyAccess\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_cfn_doc(parsed, text, "t.yaml")
        role = m["roles"][0]
        self.assertEqual(role["grants"], [{"actions": ["managed"], "on": "managed:AmazonDynamoDBReadOnlyAccess"}])

    def test_sub_shorthand_resolves_grant_target(self):
        # !Sub "${OrdersTable.Arn}/index/*" -- shorthand tag form, single var
        text = (
            "Resources:\n"
            "  OrdersTable:\n"
            "    Type: AWS::DynamoDB::Table\n"
            "    Properties:\n"
            "      TableName: orders\n"
            "  Worker:\n"
            "    Type: AWS::IAM::Role\n"
            "    Properties:\n"
            "      Policies:\n"
            "        - PolicyName: p\n"
            "          PolicyDocument:\n"
            "            Statement:\n"
            "              - Effect: Allow\n"
            "                Action: dynamodb:Query\n"
            "                Resource: !Sub \"${OrdersTable.Arn}/index/*\"\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_cfn_doc(parsed, text, "t.yaml")
        role = m["roles"][0]
        self.assertEqual(role["grants"], [{"actions": ["dynamodb:Query"], "on": "cfn:t.yaml::OrdersTable"}])

    def test_sub_longform_resolves_grant_target(self):
        # Fn::Sub long-form dict, single var
        text = (
            "Resources:\n"
            "  OrdersTable:\n"
            "    Type: AWS::DynamoDB::Table\n"
            "    Properties:\n"
            "      TableName: orders\n"
            "  Worker:\n"
            "    Type: AWS::IAM::Role\n"
            "    Properties:\n"
            "      Policies:\n"
            "        - PolicyName: p\n"
            "          PolicyDocument:\n"
            "            Statement:\n"
            "              - Effect: Allow\n"
            "                Action: dynamodb:Query\n"
            "                Resource:\n"
            "                  Fn::Sub: \"${OrdersTable.Arn}/index/*\"\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_cfn_doc(parsed, text, "t.yaml")
        role = m["roles"][0]
        self.assertEqual(role["grants"], [{"actions": ["dynamodb:Query"], "on": "cfn:t.yaml::OrdersTable"}])

    def test_sub_multi_variable_yields_both_targets(self):
        # !Sub "${OrdersTable.Arn}-${DeadLetters.Arn}" names two internal
        # resources in one string -- both must show up as "via role"
        # connections from the attached resource, not just the first match.
        text = (
            "Resources:\n"
            "  OrdersTable:\n"
            "    Type: AWS::DynamoDB::Table\n"
            "    Properties:\n"
            "      TableName: orders\n"
            "  DeadLetters:\n"
            "    Type: AWS::SQS::Queue\n"
            "    Properties:\n"
            "      QueueName: dlq\n"
            "  Worker:\n"
            "    Type: AWS::IAM::Role\n"
            "    Properties:\n"
            "      Policies:\n"
            "        - PolicyName: p\n"
            "          PolicyDocument:\n"
            "            Statement:\n"
            "              - Effect: Allow\n"
            "                Action: dynamodb:Query\n"
            "                Resource: !Sub \"${OrdersTable.Arn}-${DeadLetters.Arn}\"\n"
            "  WorkerFn:\n"
            "    Type: AWS::Lambda::Function\n"
            "    Properties:\n"
            "      Role: !GetAtt Worker.Arn\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_cfn_doc(parsed, text, "t.yaml")
        role = m["roles"][0]
        grant_targets = {g["on"] for g in role["grants"]}
        self.assertEqual(grant_targets, {"cfn:t.yaml::OrdersTable", "cfn:t.yaml::DeadLetters"})
        via_role = [c for c in m["connections"] if c["mechanism"] == "via role"]
        dsts = {c["dst"] for c in via_role}
        self.assertEqual(dsts, {"cfn:t.yaml::OrdersTable", "cfn:t.yaml::DeadLetters"})
        for c in via_role:
            self.assertEqual(c["src"], "cfn:t.yaml::WorkerFn")
            self.assertEqual(c["role"], "Worker")
            # evidence pins the statement's own start line (the "- Effect:"
            # line), same convention as the rest of the module -- not the
            # Resource: line where the Sub itself appears (line 19).
            self.assertEqual(c["evidence"], "t.yaml:17")

    def test_sam_implicit_role_from_policies(self):
        # a Serverless::Function with no explicit Role but a Policies: list
        # gets an implicit "<Fn>Role (implicit)" role attached to it
        text = (
            "Resources:\n"
            "  OrdersTable:\n"
            "    Type: AWS::DynamoDB::Table\n"
            "    Properties:\n"
            "      TableName: orders\n"
            "  MyFn:\n"
            "    Type: AWS::Serverless::Function\n"
            "    Properties:\n"
            "      Handler: index.handler\n"
            "      Policies:\n"
            "        - DynamoDBCrudPolicy:\n"
            "            TableName: !Ref OrdersTable\n"
            "        - CloudWatchPutMetricPolicy: {}\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_cfn_doc(parsed, text, "t.yaml")
        roles = {r["id"]: r for r in m["roles"]}
        irole = roles["cfn:t.yaml::MyFnRole (implicit)"]
        self.assertEqual(irole["name"], "MyFnRole (implicit)")
        self.assertEqual(irole["attached_to"], ["cfn:t.yaml::MyFn"])
        grants_by_action = {g["actions"][0]: g["on"] for g in irole["grants"]}
        self.assertEqual(grants_by_action["sam:DynamoDBCrudPolicy"], "cfn:t.yaml::OrdersTable")
        self.assertIsNone(grants_by_action["sam:CloudWatchPutMetricPolicy"])
        conns = [c for c in m["connections"] if c["mechanism"] == "via role"]
        self.assertEqual(conns, [{
            "src": "cfn:t.yaml::MyFn", "dst": "cfn:t.yaml::OrdersTable",
            "mechanism": "via role", "role": "MyFnRole (implicit)", "evidence": "t.yaml:6",
        }])
        # the Policies: block feeds the implicit role, not a second generic
        # "references" edge for the same Ref
        self.assertEqual([c for c in m["connections"] if c["mechanism"] == "references"], [])

    def test_generic_reference_between_resources(self):
        text = (
            "Resources:\n"
            "  Logs:\n"
            "    Type: AWS::S3::Bucket\n"
            "    Properties:\n"
            "      BucketName: logs\n"
            "  Shipper:\n"
            "    Type: AWS::Lambda::Function\n"
            "    Properties:\n"
            "      Environment:\n"
            "        Variables:\n"
            "          BUCKET: !Ref Logs\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_cfn_doc(parsed, text, "t.yaml")
        self.assertEqual(m["connections"], [{
            "src": "cfn:t.yaml::Shipper", "dst": "cfn:t.yaml::Logs",
            "mechanism": "references", "role": None, "evidence": "t.yaml:6",
        }])

    def test_literal_arn_resource_not_treated_as_internal(self):
        text = (
            "Resources:\n"
            "  Fn:\n"
            "    Type: AWS::Lambda::Function\n"
            "    Properties:\n"
            "      Role: arn:aws:iam::123456789012:role/existing-role\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_cfn_doc(parsed, text, "t.yaml")
        self.assertEqual(m["roles"], [])
        self.assertEqual(m["connections"], [])

    def test_no_resources_key_returns_empty(self):
        m = parse_cfn_doc({}, "", "t.yaml")
        self.assertEqual(m, {"resources": [], "roles": [], "connections": [], "deployed": {}})


class TestParseServerlessDocDirect(unittest.TestCase):
    def test_new_style_provider_iam_role_statements(self):
        text = (
            "service: svc\n"
            "provider:\n"
            "  name: aws\n"
            "  iam:\n"
            "    role:\n"
            "      statements:\n"
            "        - Effect: Allow\n"
            "          Action: s3:GetObject\n"
            "          Resource: arn:aws:s3:::bucket/*\n"
            "functions:\n"
            "  reader:\n"
            "    handler: h.reader\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_serverless_doc(parsed, text, "serverless.yml")
        role = m["roles"][0]
        self.assertEqual(role["name"], "serverless (provider)")
        self.assertEqual(role["grants"], [{"actions": ["s3:GetObject"], "on": "arn:aws:s3:::bucket/*"}])
        self.assertEqual(role["attached_to"], ["sls:serverless.yml::reader"])

    def test_embedded_resources_block_and_event_source_connection(self):
        text = (
            "service: svc\n"
            "provider:\n"
            "  name: aws\n"
            "functions:\n"
            "  handler1:\n"
            "    handler: h.handler\n"
            "    events:\n"
            "      - sqs:\n"
            "          arn: !GetAtt MyQueue.Arn\n"
            "resources:\n"
            "  Resources:\n"
            "    MyQueue:\n"
            "      Type: AWS::SQS::Queue\n"
            "      Properties:\n"
            "        QueueName: q\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_serverless_doc(parsed, text, "serverless.yml")
        ids = {r["id"] for r in m["resources"]}
        self.assertIn("cfn:serverless.yml::MyQueue", ids)
        self.assertIn("sls:serverless.yml::handler1", ids)
        self.assertEqual(m["connections"], [{
            "src": "cfn:serverless.yml::MyQueue", "dst": "sls:serverless.yml::handler1",
            "mechanism": "event source", "role": None, "evidence": "serverless.yml:5",
        }])

    def test_function_with_own_role_excluded_from_provider_attachment(self):
        text = (
            "service: svc\n"
            "provider:\n"
            "  name: aws\n"
            "  iamRoleStatements:\n"
            "    - Effect: Allow\n"
            "      Action: s3:GetObject\n"
            "      Resource: arn:aws:s3:::bucket/*\n"
            "functions:\n"
            "  reader:\n"
            "    handler: h.reader\n"
            "  writer:\n"
            "    handler: h.writer\n"
            "    role: arn:aws:iam::123456789012:role/custom-writer-role\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_serverless_doc(parsed, text, "serverless.yml")
        role = m["roles"][0]
        self.assertEqual(role["attached_to"], ["sls:serverless.yml::reader"])

    def test_no_functions_returns_empty(self):
        m = parse_serverless_doc({"service": "svc", "functions": {}}, "", "serverless.yml")
        self.assertEqual(m, {"resources": [], "roles": [], "connections": [], "deployed": {}})


class TestIsComposeDoc(unittest.TestCase):
    def test_true_for_docker_compose_yml_with_services(self):
        self.assertTrue(is_compose_doc("docker-compose.yml", {"services": {"web": {}}}))

    def test_true_for_bare_compose_yaml_with_services(self):
        self.assertTrue(is_compose_doc("compose.yaml", {"services": {"web": {}}}))
        self.assertTrue(is_compose_doc("compose.override.yml", {"services": {"web": {}}}))

    def test_false_without_services_key(self):
        self.assertFalse(is_compose_doc("docker-compose.yml", {"version": "3"}))

    def test_false_with_empty_services(self):
        self.assertFalse(is_compose_doc("docker-compose.yml", {"services": {}}))

    def test_false_for_unrelated_filename(self):
        self.assertFalse(is_compose_doc("template.yaml", {"services": {"web": {}}}))

    def test_false_for_non_dict(self):
        self.assertFalse(is_compose_doc("docker-compose.yml", None))


class TestSplitYamlDocs(unittest.TestCase):
    def test_single_doc_no_separator(self):
        docs = split_yaml_docs("a: 1\nb: 2\n")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0], ("a: 1\nb: 2\n", 1))

    def test_two_docs_split_on_bare_dashes_line(self):
        text = "kind: A\n---\nkind: B\n"
        docs = split_yaml_docs(text)
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0][0], "kind: A")
        self.assertEqual(docs[0][1], 1)
        self.assertEqual(docs[1][0], "kind: B\n")
        self.assertEqual(docs[1][1], 3)

    def test_leading_separator_produces_no_empty_first_doc(self):
        text = "---\nkind: A\n"
        docs = split_yaml_docs(text)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0], ("kind: A\n", 2))

    def test_app_yaml_fixture_splits_into_six_docs(self):
        # the k8s fixture used by TestScanIacK8s below: Deployment, Service,
        # ServiceAccount, Role, RoleBinding, ConfigMap
        text = (FIX / "k8s" / "app.yaml").read_text()
        docs = split_yaml_docs(text)
        self.assertEqual(len(docs), 6)
        kinds = [parse_yaml_lite(chunk).get("kind") for chunk, _ in docs]
        self.assertEqual(kinds, ["Deployment", "Service", "ServiceAccount", "Role", "RoleBinding", "ConfigMap"])


class TestParseDocUnrecognized(unittest.TestCase):
    def test_yaml_file_with_no_services_or_kind_returns_none(self):
        text = "just_some_key: some_value\nother: 1\n"
        self.assertIsNone(parse_iac_k8s_doc("random.yaml", "random.yaml", text, parse_yaml_lite(text)))


class TestScanIacCompose(unittest.TestCase):
    """docker-compose.yml: web depends_on/links redis, both mount a shared
    named volume, web's REDIS_URL env value dns-references redis by exact
    hostname. Ids are "compose:<file>::<service name>"."""

    def test_service_resources(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"] if r["id"].startswith("compose:")}
        self.assertEqual(len(by_id), 2)
        web = by_id["compose:docker-compose.yml::web"]
        self.assertEqual(web["rtype"], "compose service")
        self.assertEqual(web["name"], "web")
        self.assertEqual(web["source_file"], "docker-compose.yml")
        self.assertEqual(web["line"], 2)
        redis = by_id["compose:docker-compose.yml::redis"]
        self.assertEqual(redis["line"], 13)

    def test_depends_on_connection(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "depends on"]
        self.assertEqual(conns, [{
            "src": "compose:docker-compose.yml::web", "dst": "compose:docker-compose.yml::redis",
            "mechanism": "depends on", "role": None, "evidence": "docker-compose.yml:4",
        }])

    def test_links_connection(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "links"]
        self.assertEqual(conns, [{
            "src": "compose:docker-compose.yml::web", "dst": "compose:docker-compose.yml::redis",
            "mechanism": "links", "role": None, "evidence": "docker-compose.yml:6",
        }])

    def test_shared_volume_connection_evidenced_at_later_service(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "shared volume"]
        # web is declared first, redis second -- evidence points at redis's
        # own volume-mount line, not web's
        self.assertEqual(conns, [{
            "src": "compose:docker-compose.yml::web", "dst": "compose:docker-compose.yml::redis",
            "mechanism": "shared volume", "role": None, "evidence": "docker-compose.yml:16",
        }])

    def test_network_dns_connection_from_env_url(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "network dns"]
        self.assertEqual(conns, [{
            "src": "compose:docker-compose.yml::web", "dst": "compose:docker-compose.yml::redis",
            "mechanism": "network dns", "role": None, "evidence": "docker-compose.yml:11",
        }])

    def test_every_mechanism_present_exactly_once(self):
        m = scan_iac(FIX)
        mechanisms = [c["mechanism"] for c in m["connections"]
                      if c["src"].startswith("compose:") or c["dst"].startswith("compose:")]
        self.assertEqual(sorted(mechanisms), ["depends on", "links", "network dns", "shared volume"])

    def test_no_roles_from_compose(self):
        m = scan_iac(FIX)
        self.assertEqual([r for r in m["roles"] if r["id"].startswith("compose:")], [])


class TestScanIacK8s(unittest.TestCase):
    """k8s/app.yaml: a multi-doc manifest (Deployment, Service, ServiceAccount,
    Role, RoleBinding, ConfigMap). Workload/Service/ServiceAccount/ConfigMap
    ids are "k8s:<file>::<kind>/<name>"; the ServiceAccount's role identity
    is the distinct id "k8s:<file>::sa/<name>" (Role/RoleBinding feed grants,
    they are not boxes -- same convention as a CFN inline policy doc)."""

    def test_workload_service_sa_configmap_are_resources(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"] if r["id"].startswith("k8s:")}
        self.assertEqual(len(by_id), 4)
        dep = by_id["k8s:k8s/app.yaml::Deployment/app"]
        self.assertEqual(dep["rtype"], "Deployment")
        self.assertEqual(dep["source_file"], "k8s/app.yaml")
        self.assertEqual(dep["line"], 4)
        svc = by_id["k8s:k8s/app.yaml::Service/app-svc"]
        self.assertEqual(svc["rtype"], "Service")
        self.assertEqual(svc["line"], 28)
        sa = by_id["k8s:k8s/app.yaml::ServiceAccount/app-sa"]
        self.assertEqual(sa["rtype"], "ServiceAccount")
        self.assertEqual(sa["line"], 38)
        cm = by_id["k8s:k8s/app.yaml::ConfigMap/app-config"]
        self.assertEqual(cm["rtype"], "ConfigMap")
        self.assertEqual(cm["line"], 67)

    def test_role_and_rolebinding_are_not_resources(self):
        m = scan_iac(FIX)
        resource_ids = {r["id"] for r in m["resources"]}
        self.assertNotIn("k8s:k8s/app.yaml::Role/app-role", resource_ids)
        self.assertNotIn("k8s:k8s/app.yaml::RoleBinding/app-role-binding", resource_ids)

    def test_service_account_role_has_rbac_grants_and_attached_to(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"] if r["id"].startswith("k8s:")}
        self.assertEqual(len(roles), 1)
        role = roles["k8s:k8s/app.yaml::sa/app-sa"]
        self.assertEqual(role["name"], "app-sa")
        self.assertEqual(role["source_file"], "k8s/app.yaml")
        self.assertEqual(role["line"], 38)
        self.assertEqual(role["grants"], [{"actions": ["get", "list"], "on": "/configmaps"}])
        self.assertEqual(role["attached_to"], ["k8s:k8s/app.yaml::Deployment/app"])

    def test_runs_as_connection_targets_the_service_account_resource(self):
        # divergence from "via role": a k8s grant's "on" names an API group,
        # not a repo box, so there's no specific granted target to point at.
        # dst still follows the cross-parser contract that every connection
        # dst is a resource id -- it falls back to the ServiceAccount's own
        # box (k8s:...::ServiceAccount/app-sa, NOT the sa/ role-identity id,
        # which is not a resource); role is the bare SA name, matching the
        # TF/CFN convention of role["name"] rather than an internal id.
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "runs as"]
        self.assertEqual(conns, [{
            "src": "k8s:k8s/app.yaml::Deployment/app", "dst": "k8s:k8s/app.yaml::ServiceAccount/app-sa",
            "mechanism": "runs as", "role": "app-sa",
            "evidence": "k8s/app.yaml:17",
        }])

    def test_selects_connection_from_label_match(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "selects"]
        self.assertEqual(conns, [{
            "src": "k8s:k8s/app.yaml::Service/app-svc", "dst": "k8s:k8s/app.yaml::Deployment/app",
            "mechanism": "selects", "role": None, "evidence": "k8s/app.yaml:30",
        }])

    def test_mounts_connection_from_env_from_config_map_ref(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "mounts"]
        self.assertEqual(conns, [{
            "src": "k8s:k8s/app.yaml::Deployment/app", "dst": "k8s:k8s/app.yaml::ConfigMap/app-config",
            "mechanism": "mounts", "role": None, "evidence": "k8s/app.yaml:23",
        }])

    def test_every_mechanism_present_exactly_once(self):
        m = scan_iac(FIX)
        mechanisms = [c["mechanism"] for c in m["connections"]
                      if c["src"].startswith("k8s:") or c["dst"].startswith("k8s:")]
        self.assertEqual(sorted(mechanisms), ["mounts", "runs as", "selects"])


class TestConnectionDstInvariant(unittest.TestCase):
    """Standing cross-parser contract: every connection's dst must be an id
    present in `resources` -- a connection dangling to a non-existent box is
    a broken edge in the System-tab UI. Checked across the WHOLE merged
    iacrepo fixture (Terraform + CFN/SAM + serverless.yml + compose + k8s)
    so a future parser can't reintroduce a dangling edge unnoticed. Add new
    fixture content to iacrepo/ as parsers grow rather than special-casing
    a gap here."""

    def test_all_connection_dsts_are_resource_ids(self):
        m = scan_iac(FIX)
        resource_ids = {r["id"] for r in m["resources"]}
        dangling = [c for c in m["connections"] if c["dst"] not in resource_ids]
        self.assertEqual(dangling, [], f"connections with dst not in resources: {dangling}")


class TestSplitYamlDocsBlockScalarSeparator(unittest.TestCase):
    """A '---' line only separates documents at column 0, and never while
    inside a '|'/'>' block scalar -- its content is opaque text that can
    legitimately contain an indented '---' (e.g. a markdown rule inside a
    ConfigMap's data field)."""

    def test_indented_dashes_inside_block_scalar_do_not_split(self):
        text = (
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "data:\n"
            "  notes: |\n"
            "    line one\n"
            "      ---\n"
            "    line two\n"
            "metadata:\n"
            "  name: notes-config\n"
        )
        docs = split_yaml_docs(text)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0][1], 1)
        parsed = parse_yaml_lite(docs[0][0])
        self.assertEqual(parsed["kind"], "ConfigMap")
        self.assertEqual(parsed["data"]["notes"], "line one\n  ---\nline two")
        self.assertEqual(parsed["metadata"]["name"], "notes-config")

    def test_configmap_resource_recovered_when_block_scalar_precedes_metadata(self):
        # same fixture as above, exercised through the full parse_doc path:
        # before the fix, the indented "---" split this into two bogus
        # documents (one with data but no metadata.name, one with neither
        # kind nor metadata), and the ConfigMap resource silently vanished.
        text = (
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "data:\n"
            "  notes: |\n"
            "    line one\n"
            "      ---\n"
            "    line two\n"
            "metadata:\n"
            "  name: notes-config\n"
        )
        m = parse_iac_k8s_doc("notes.yaml", "notes.yaml", text, {})
        self.assertIsNotNone(m)
        ids = {r["id"] for r in m["resources"]}
        self.assertIn("k8s:notes.yaml::ConfigMap/notes-config", ids)

    def test_real_column_zero_separator_still_splits(self):
        text = "kind: A\nmetadata:\n  name: a\n---\nkind: B\nmetadata:\n  name: b\n"
        docs = split_yaml_docs(text)
        self.assertEqual(len(docs), 2)

    def test_indented_dashes_outside_any_block_scalar_do_not_split_either(self):
        # column 0 is required regardless of block-scalar state
        text = "kind: A\nmetadata:\n  name: a\n  fake: ---\n"
        docs = split_yaml_docs(text)
        self.assertEqual(len(docs), 1)


class TestNetworkDnsHostExtraction(unittest.TestCase):
    """URL_HOST_RE must resolve the actual host, not userinfo before an '@',
    and the exact-hostname match must never fire on an unrelated value that
    merely contains a service name as a substring."""

    def test_userinfo_in_url_does_not_collide_with_username(self):
        # real host is "realhost"; the userinfo segment "db:secret" must not
        # be mistaken for a reference to a same-named "db" service
        text = (
            "services:\n"
            "  db:\n"
            "    image: postgres\n"
            "  app:\n"
            "    image: myorg/app\n"
            "    environment:\n"
            "      DATABASE_URL: http://db:secret@realhost/path\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_compose_doc(parsed, text, "docker-compose.yml")
        dns = [c for c in m["connections"] if c["mechanism"] == "network dns"]
        self.assertEqual(dns, [])

    def test_substring_hostname_does_not_match_shorter_service_name(self):
        # "mydb.internal" must not be treated as a reference to service "db"
        text = (
            "services:\n"
            "  db:\n"
            "    image: postgres\n"
            "  app:\n"
            "    image: myorg/app\n"
            "    environment:\n"
            "      DATABASE_URL: http://mydb.internal/path\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_compose_doc(parsed, text, "docker-compose.yml")
        dns = [c for c in m["connections"] if c["mechanism"] == "network dns"]
        self.assertEqual(dns, [])

    def test_exact_hostname_match_still_connects(self):
        # sanity check the fix didn't break the happy path
        text = (
            "services:\n"
            "  db:\n"
            "    image: postgres\n"
            "  app:\n"
            "    image: myorg/app\n"
            "    environment:\n"
            "      DATABASE_URL: http://db:5432/path\n"
        )
        parsed = parse_yaml_lite(text)
        m = parse_compose_doc(parsed, text, "docker-compose.yml")
        dns = [c for c in m["connections"] if c["mechanism"] == "network dns"]
        self.assertEqual(len(dns), 1)
        self.assertEqual(dns[0]["dst"], "compose:docker-compose.yml::db")


class TestScanIacDecoyYamlFiles(unittest.TestCase):
    """Non-IaC yaml (a GitHub Actions workflow, an mkdocs site config) must
    never be mistaken for compose/k8s/CFN/serverless content. Dropped into a
    fresh tempdir, not iacrepo/, so this doesn't perturb any pinned count."""

    def test_github_workflow_and_mkdocs_yaml_produce_empty_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wf_dir = root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "deploy.yml").write_text(
                "name: Deploy\n"
                "on:\n"
                "  push:\n"
                "    branches:\n"
                "      - main\n"
                "jobs:\n"
                "  deploy:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - run: echo deploying\n"
            )
            (root / "mkdocs.yml").write_text(
                "site_name: My Docs\n"
                "nav:\n"
                "  - Home: index.md\n"
                "  - Guide: guide.md\n"
                "theme:\n"
                "  name: material\n"
            )
            m = scan_iac(root)
            self.assertEqual(m, {"resources": [], "roles": [], "connections": [], "deployed": {}})


class TestIsCdkSource(unittest.TestCase):
    """Cheap import-based gate: a .ts/.py file is only handed to the CDK
    parser if it imports aws-cdk-lib / aws_cdk. Never invent CDK structure
    out of an unrelated file that merely has a similar shape."""

    def test_ts_source_with_aws_cdk_lib_import_recognized(self):
        text = (Path(__file__).parent / "fixtures" / "iacrepo" / "cdk" / "stack.ts").read_text()
        self.assertTrue(is_cdk_ts(text))

    def test_ts_source_without_import_not_recognized(self):
        text = 'export class Widget {\n  constructor() {\n    new Something(this, "X", {});\n  }\n}\n'
        self.assertFalse(is_cdk_ts(text))

    def test_py_source_with_aws_cdk_import_recognized(self):
        text = (Path(__file__).parent / "fixtures" / "iacrepo" / "cdk" / "stack.py").read_text()
        self.assertTrue(is_cdk_py(text))

    def test_py_source_without_import_not_recognized(self):
        text = "class Widget:\n    def __init__(self):\n        pass\n"
        self.assertFalse(is_cdk_py(text))


class TestScanIacCdkTs(unittest.TestCase):
    """tests/fixtures/iacrepo/cdk/stack.ts: Function + Table + Bucket +
    Queue, table.grantReadWriteData(fn), bucket.grantRead(fn), and an
    explicit iam.Role (ExecRole) attached to a second Function (Notifier)
    via a `role:` prop, granted queue access via addToPolicy."""

    def test_constructs_pinned_with_lines(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"] if r["source_file"] == "cdk/stack.ts"}
        self.assertEqual(len(by_id), 6)
        expected = {
            "cdk:cdk/stack.ts::Worker": ("cdk lambda", 13),
            "cdk:cdk/stack.ts::Orders": ("cdk dynamodb", 19),
            "cdk:cdk/stack.ts::Uploads": ("cdk s3", 23),
            "cdk:cdk/stack.ts::DeadLetter": ("cdk sqs", 25),
            "cdk:cdk/stack.ts::ExecRole": ("cdk iam role", 30),
            "cdk:cdk/stack.ts::Notifier": ("cdk lambda", 38),
        }
        for rid, (rtype, line) in expected.items():
            r = by_id[rid]
            self.assertEqual(r["rtype"], rtype)
            self.assertEqual(r["line"], line)
            self.assertEqual(r["attrs"], {"confidence": "best-effort"})

    def test_grant_connections(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"].startswith("cdk grant:")
                 and c["evidence"].startswith("cdk/stack.ts:")]
        self.assertEqual(sorted(conns, key=lambda c: c["evidence"]), [
            {"src": "cdk:cdk/stack.ts::Worker", "dst": "cdk:cdk/stack.ts::Orders",
             "mechanism": "cdk grant: grantReadWriteData", "role": None,
             "evidence": "cdk/stack.ts:27"},
            {"src": "cdk:cdk/stack.ts::Worker", "dst": "cdk:cdk/stack.ts::Uploads",
             "mechanism": "cdk grant: grantRead", "role": None,
             "evidence": "cdk/stack.ts:28"},
        ])

    def test_implicit_role_from_grants(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        role = roles["cdk:cdk/stack.ts::role/Worker (implicit)"]
        self.assertEqual(role["name"], "Worker (implicit)")
        self.assertEqual(role["attached_to"], ["cdk:cdk/stack.ts::Worker"])
        self.assertEqual(role["grants"], [
            {"actions": ["dynamodb:readwrite (cdk)"], "on": "cdk:cdk/stack.ts::Orders"},
            {"actions": ["s3:read (cdk)"], "on": "cdk:cdk/stack.ts::Uploads"},
        ])

    def test_explicit_role_prop_and_add_to_policy(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        role = roles["cdk:cdk/stack.ts::ExecRole"]
        self.assertEqual(role["name"], "ExecRole")
        self.assertEqual(role["attached_to"], ["cdk:cdk/stack.ts::Notifier"])
        self.assertEqual(role["grants"], [
            {"actions": ["sqs:SendMessage"], "on": "cdk:cdk/stack.ts::DeadLetter"},
        ])

    def test_via_role_connection_from_add_to_policy(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["mechanism"] == "via role"
                 and c["evidence"].startswith("cdk/stack.ts:")]
        self.assertEqual(conns, [{
            "src": "cdk:cdk/stack.ts::Notifier", "dst": "cdk:cdk/stack.ts::DeadLetter",
            "mechanism": "via role", "role": "ExecRole", "evidence": "cdk/stack.ts:33",
        }])


class TestScanIacCdkPy(unittest.TestCase):
    """tests/fixtures/iacrepo/cdk/stack.py: mirrors the TS fixture with a
    smaller shape -- 2 constructs (Function + Table) and 1 grant call using
    the Python CDK's snake_case grant method name."""

    def test_constructs_pinned_with_lines(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"] if r["source_file"] == "cdk/stack.py"}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id["cdk:cdk/stack.py::Worker"]["rtype"], "cdk lambda")
        self.assertEqual(by_id["cdk:cdk/stack.py::Worker"]["line"], 11)
        self.assertEqual(by_id["cdk:cdk/stack.py::Orders"]["rtype"], "cdk dynamodb")
        self.assertEqual(by_id["cdk:cdk/stack.py::Orders"]["line"], 18)
        for r in by_id.values():
            self.assertEqual(r["attrs"], {"confidence": "best-effort"})

    def test_snake_case_grant_connection(self):
        m = scan_iac(FIX)
        conns = [c for c in m["connections"] if c["evidence"].startswith("cdk/stack.py:")]
        self.assertEqual(conns, [{
            "src": "cdk:cdk/stack.py::Worker", "dst": "cdk:cdk/stack.py::Orders",
            "mechanism": "cdk grant: grant_read_write_data", "role": None,
            "evidence": "cdk/stack.py:23",
        }])

    def test_implicit_role_from_snake_case_grant(self):
        m = scan_iac(FIX)
        roles = {r["id"]: r for r in m["roles"]}
        role = roles["cdk:cdk/stack.py::role/Worker (implicit)"]
        self.assertEqual(role["attached_to"], ["cdk:cdk/stack.py::Worker"])
        self.assertEqual(role["grants"], [
            {"actions": ["dynamodb:readwrite (cdk)"], "on": "cdk:cdk/stack.py::Orders"},
        ])


class TestParseCdkSnippets(unittest.TestCase):
    """Direct unit tests against parse_cdk_ts/parse_cdk_py on small inline
    snippets, independent of the shared fixture."""

    def test_parse_cdk_ts_unbound_construct_still_a_resource(self):
        # a construct with no `const x =` binding is still a box; it just
        # can't be a grantX target/grantee or a role attachment src later.
        text = (
            "import * as s3 from 'aws-cdk-lib/aws-s3';\n"
            "new s3.Bucket(this, \"Anon\");\n"
        )
        m = parse_cdk_ts(text, "snippet.ts")
        self.assertEqual(m["resources"], [{
            "id": "cdk:snippet.ts::Anon", "rtype": "cdk s3", "name": "Anon",
            "source_file": "snippet.ts", "line": 2, "attrs": {"confidence": "best-effort"},
        }])
        self.assertEqual(m["roles"], [])
        self.assertEqual(m["connections"], [])

    def test_parse_cdk_ts_unrecognized_construct_family_skipped(self):
        # never invent: a construct class whose suffix isn't in the family
        # map is not emitted as a resource at all.
        text = "new somewhere.WeirdThing(this, \"X\", {});\n"
        m = parse_cdk_ts(text, "snippet.ts")
        self.assertEqual(m["resources"], [])

    def test_parse_cdk_py_grant_without_resolvable_grantee_produces_no_connection(self):
        # grantee var never bound to a construct -> best-effort no-op, not a
        # guess.
        text = (
            "from aws_cdk import aws_dynamodb as dynamodb\n"
            "table = dynamodb.Table(self, \"Orders\")\n"
            "table.grant_read_data(unknown_fn)\n"
        )
        m = parse_cdk_py(text, "snippet.py")
        self.assertEqual(m["connections"], [])
        self.assertEqual(m["roles"], [])


class TestCdkDecoyNonCdkSourceYieldsNothing(unittest.TestCase):
    """A .ts file that doesn't import aws-cdk-lib must never be parsed as
    CDK, even if it happens to contain a similarly-shaped `new X(this, ...)`
    call -- the cheap import gate is the whole guard against false
    positives on arbitrary TypeScript classes."""

    def test_decoy_ts_file_in_tempdir_yields_empty_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widget.ts").write_text(
                "export class Widget {\n"
                "  constructor(scope: any, id: string) {\n"
                "    const thing = new lambda.Function(this, \"NotReal\", {});\n"
                "    thing.grantInvoke(other);\n"
                "  }\n"
                "}\n"
            )
            m = scan_iac(root)
            self.assertEqual(m, {"resources": [], "roles": [], "connections": [], "deployed": {}})


class TestResourceIdUniqueness(unittest.TestCase):
    """Standing invariant across every parser: an id names exactly one thing.

    Two different declarations sharing one id is the defect class this pins:
    the template indexes boxes by id (`byId[r.id]`), so a collision silently
    overwrites a box, stacks two boxes at one set of coordinates, and shows
    only the last writer's grants. Checked over the WHOLE merged iacrepo
    fixture so any new parser (or any change to an existing id scheme) has to
    keep the guarantee. A role id legitimately equals a resource id when both
    describe the SAME declaration (an aws_iam_role is a box AND a role) --
    that's asserted here as same-file/same-line rather than exempted."""

    def test_resource_ids_are_unique(self):
        m = scan_iac(FIX)
        for key in ("resources", "roles"):
            ids = [r["id"] for r in m[key]]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            self.assertEqual(dupes, [], f"duplicate ids within {key}: {dupes}")
        by_res = {r["id"]: r for r in m["resources"]}
        for role in m["roles"]:
            res = by_res.get(role["id"])
            if res is None:
                continue
            self.assertEqual(
                (res["source_file"], res["line"]), (role["source_file"], role["line"]),
                f"id {role['id']} names two different declarations",
            )


class TestTerraformMultiEnvIds(unittest.TestCase):
    """Reviewer repro: a multi-env repo declares the same resource names in
    envs/prod and envs/dev. Terraform ids are qualified by the .tf file's
    DIRECTORY (its module boundary), so the two environments never collide.

    Before that qualification both directories produced `aws_s3_bucket.logs`
    and `aws_iam_role.app`: duplicate ids, two boxes at identical layout
    coordinates, and only the last-parsed file's grants surviving on the
    shared role box. Model-level -- no template needed to prove it."""

    ENV_TF = (
        'resource "aws_s3_bucket" "logs" {{\n'
        '  bucket = "{env}-logs"\n'
        '}}\n\n'
        'resource "aws_iam_role" "app" {{\n'
        '  name = "{env}-app"\n'
        '}}\n\n'
        'resource "aws_iam_role_policy" "app_policy" {{\n'
        '  role = aws_iam_role.app.id\n'
        '  policy = jsonencode({{\n'
        '    Statement = [\n'
        '      {{\n'
        '        Effect   = "Allow"\n'
        '        Action   = ["{action}"]\n'
        '        Resource = aws_s3_bucket.logs.arn\n'
        '      }}\n'
        '    ]\n'
        '  }})\n'
        '}}\n\n'
        'resource "aws_lambda_function" "worker" {{\n'
        '  function_name = "{env}-worker"\n'
        '  role          = aws_iam_role.app.arn\n'
        '}}\n'
    )

    def _two_env_repo(self, td):
        root = Path(td)
        for env, action in (("prod", "s3:PutObject"), ("dev", "s3:GetObject")):
            d = root / "envs" / env
            d.mkdir(parents=True)
            (d / "main.tf").write_text(self.ENV_TF.format(env=env, action=action))
        return root

    def test_same_names_in_two_dirs_get_distinct_ids(self):
        with tempfile.TemporaryDirectory() as td:
            m = scan_iac(self._two_env_repo(td))
            ids = [r["id"] for r in m["resources"]]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate resource ids: {ids}")
            self.assertIn("tf:envs/prod::aws_s3_bucket.logs", ids)
            self.assertIn("tf:envs/dev::aws_s3_bucket.logs", ids)
            self.assertIn("tf:envs/prod::aws_lambda_function.worker", ids)
            self.assertIn("tf:envs/dev::aws_lambda_function.worker", ids)

    def test_both_environments_keep_their_own_grants(self):
        with tempfile.TemporaryDirectory() as td:
            m = scan_iac(self._two_env_repo(td))
            roles = {r["id"]: r for r in m["roles"]}
            self.assertEqual(len(m["roles"]), 2)
            prod = roles["tf:envs/prod::aws_iam_role.app"]
            dev = roles["tf:envs/dev::aws_iam_role.app"]
            # display names stay bare: the qualification is an id concern only
            self.assertEqual(prod["name"], "app")
            self.assertEqual(dev["name"], "app")
            self.assertEqual(prod["grants"], [
                {"actions": ["s3:PutObject"], "on": "tf:envs/prod::aws_s3_bucket.logs"}])
            self.assertEqual(dev["grants"], [
                {"actions": ["s3:GetObject"], "on": "tf:envs/dev::aws_s3_bucket.logs"}])
            self.assertEqual(prod["attached_to"], ["tf:envs/prod::aws_lambda_function.worker"])
            self.assertEqual(dev["attached_to"], ["tf:envs/dev::aws_lambda_function.worker"])

    def test_no_cross_environment_connections(self):
        with tempfile.TemporaryDirectory() as td:
            m = scan_iac(self._two_env_repo(td))
            self.assertEqual(len(m["connections"]), 2)
            for c in m["connections"]:
                env = "prod" if "prod" in c["src"] else "dev"
                self.assertTrue(c["src"].startswith(f"tf:envs/{env}::"))
                self.assertTrue(c["dst"].startswith(f"tf:envs/{env}::"),
                                f"connection crosses environments: {c}")

    def test_root_level_tf_uses_dot_dirpath(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "main.tf").write_text(self.ENV_TF.format(env="root", action="s3:GetObject"))
            m = scan_iac(root)
            self.assertIn("tf:.::aws_s3_bucket.logs", {r["id"] for r in m["resources"]})


class TestTerraformSameDirCrossFile(unittest.TestCase):
    """A directory IS Terraform's module boundary, which is exactly why the
    id qualifier is the directory and not the file: main.tf and iam.tf in one
    folder are one module and reference each other's addresses freely. So
    scan_iac hands a whole directory to the parser at once, and a role
    declared in iam.tf resolves for a resource declared in main.tf."""

    def _module(self, td):
        root = Path(td) / "infra"
        root.mkdir(parents=True)
        (root / "iam.tf").write_text(
            'resource "aws_iam_role" "worker" {\n'
            '  name = "worker-role"\n'
            '}\n\n'
            'resource "aws_iam_role_policy" "worker_policy" {\n'
            '  role = aws_iam_role.worker.id\n'
            '  policy = jsonencode({\n'
            '    Statement = [\n'
            '      {\n'
            '        Effect   = "Allow"\n'
            '        Action   = ["dynamodb:Query"]\n'
            '        Resource = aws_dynamodb_table.orders.arn\n'
            '      }\n'
            '    ]\n'
            '  })\n'
            '}\n'
        )
        (root / "main.tf").write_text(
            'resource "aws_dynamodb_table" "orders" {\n'
            '  name = "orders"\n'
            '}\n\n'
            'resource "aws_lambda_function" "worker" {\n'
            '  function_name = "worker"\n'
            '  role          = aws_iam_role.worker.arn\n'
            '}\n'
        )
        return Path(td)

    def test_role_in_one_file_attaches_to_resource_in_another(self):
        with tempfile.TemporaryDirectory() as td:
            m = scan_iac(self._module(td))
            role = {r["id"]: r for r in m["roles"]}["tf:infra::aws_iam_role.worker"]
            self.assertEqual(role["source_file"], "infra/iam.tf")
            self.assertEqual(role["attached_to"], ["tf:infra::aws_lambda_function.worker"])
            self.assertEqual(role["grants"], [
                {"actions": ["dynamodb:Query"], "on": "tf:infra::aws_dynamodb_table.orders"}])

    def test_cross_file_via_role_connection_with_evidence_from_the_policy_file(self):
        with tempfile.TemporaryDirectory() as td:
            m = scan_iac(self._module(td))
            self.assertEqual(m["connections"], [{
                "src": "tf:infra::aws_lambda_function.worker",
                "dst": "tf:infra::aws_dynamodb_table.orders",
                "mechanism": "via role", "role": "worker",
                "evidence": "infra/iam.tf:9",
            }])


class TestGroupOf(unittest.TestCase):
    """group_of pins the human-readable "source group" for each parser's id
    scheme -- what the System tab's environment chips and badges key off.
    Terraform groups by module directory (the "." root becomes the readable
    "terraform", never a bare dot); every file-qualified parser (cfn/sls/
    compose/k8s/cdk) groups by its source file with the extension stripped."""

    def test_terraform_root_dir_reads_as_terraform(self):
        self.assertEqual(group_of("tf:.::aws_s3_bucket.logs"), "terraform")

    def test_terraform_nested_dir_is_the_dirpath(self):
        self.assertEqual(group_of("tf:envs/prod::aws_iam_role.app"), "envs/prod")

    def test_cfn_group_is_file_minus_extension(self):
        self.assertEqual(group_of("cfn:template.yaml::OrdersTable"), "template")

    def test_serverless_group_is_file_minus_extension(self):
        self.assertEqual(group_of("sls:serverless.yml::uploadReceipt"), "serverless")

    def test_compose_group_is_file_minus_extension(self):
        self.assertEqual(group_of("compose:docker-compose.yml::web"), "docker-compose")

    def test_k8s_group_is_file_minus_extension_keeping_its_subdir(self):
        self.assertEqual(group_of("k8s:k8s/app.yaml::Deployment/app"), "k8s/app")

    def test_cdk_group_is_file_minus_extension_keeping_its_subdir(self):
        self.assertEqual(group_of("cdk:cdk/stack.ts::OrdersStack"), "cdk/stack")

    def test_malformed_or_empty_id_yields_empty_group(self):
        self.assertEqual(group_of(""), "")
        self.assertEqual(group_of(None), "")
        self.assertEqual(group_of("not-a-qualified-id"), "")
        self.assertEqual(group_of("tf:missing-bare-part"), "")


class TestScanIacBakesGroupPerParser(unittest.TestCase):
    """scan_iac bakes "group" onto every resource/role dict (task: environment
    visibility), one assertion per parser so a future id-scheme change to any
    single parser can't silently drop its group without a test noticing."""

    def test_group_present_on_every_resource_and_role(self):
        m = scan_iac(FIX)
        for r in m["resources"]:
            self.assertIn("group", r, r["id"])
            self.assertTrue(r["group"], f"empty group for {r['id']}")
        for r in m["roles"]:
            self.assertIn("group", r, r["id"])
            self.assertTrue(r["group"], f"empty group for {r['id']}")

    def test_terraform_group_is_terraform_at_repo_root(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"]}
        self.assertEqual(by_id["tf:.::aws_lambda_function.process_order"]["group"], "terraform")

    def test_cfn_group_is_the_template_file(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"]}
        cfn_ids = [rid for rid in by_id if rid.startswith("cfn:template.yaml::")]
        self.assertTrue(cfn_ids)
        for rid in cfn_ids:
            self.assertEqual(by_id[rid]["group"], "template")

    def test_serverless_group_is_the_serverless_file(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"]}
        sls_ids = [rid for rid in by_id if rid.startswith("sls:serverless.yml::")]
        self.assertTrue(sls_ids)
        for rid in sls_ids:
            self.assertEqual(by_id[rid]["group"], "serverless")

    def test_compose_group_is_the_compose_file(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"]}
        compose_ids = [rid for rid in by_id if rid.startswith("compose:docker-compose.yml::")]
        self.assertTrue(compose_ids)
        for rid in compose_ids:
            self.assertEqual(by_id[rid]["group"], "docker-compose")

    def test_k8s_group_is_the_manifest_file(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"]}
        k8s_ids = [rid for rid in by_id if rid.startswith("k8s:k8s/app.yaml::")]
        self.assertTrue(k8s_ids)
        for rid in k8s_ids:
            self.assertEqual(by_id[rid]["group"], "k8s/app")

    def test_cdk_group_is_the_stack_source_file(self):
        m = scan_iac(FIX)
        by_id = {r["id"]: r for r in m["resources"]}
        ts_ids = [rid for rid in by_id if rid.startswith("cdk:cdk/stack.ts::")]
        py_ids = [rid for rid in by_id if rid.startswith("cdk:cdk/stack.py::")]
        self.assertTrue(ts_ids)
        self.assertTrue(py_ids)
        for rid in ts_ids + py_ids:
            self.assertEqual(by_id[rid]["group"], "cdk/stack")


class TestTerraformMultiEnvGroups(unittest.TestCase):
    """Same reviewer repro (envs/prod vs envs/dev, same resource names) used
    by TestTerraformMultiEnvIds above, this time pinning that the two
    environments land in distinct GROUPS -- what actually drives the System
    tab's environment chips, on top of the id-uniqueness that class already
    covers. Not a subclass of TestTerraformMultiEnvIds on purpose: subclassing
    would re-run its test_* methods a second time under this name."""

    def _two_env_repo(self, td):
        root = Path(td)
        for env, action in (("prod", "s3:PutObject"), ("dev", "s3:GetObject")):
            d = root / "envs" / env
            d.mkdir(parents=True)
            (d / "main.tf").write_text(TestTerraformMultiEnvIds.ENV_TF.format(env=env, action=action))
        return root

    def test_prod_and_dev_get_distinct_groups(self):
        with tempfile.TemporaryDirectory() as td:
            m = scan_iac(self._two_env_repo(td))
            by_id = {r["id"]: r for r in m["resources"]}
            self.assertEqual(by_id["tf:envs/prod::aws_s3_bucket.logs"]["group"], "envs/prod")
            self.assertEqual(by_id["tf:envs/dev::aws_s3_bucket.logs"]["group"], "envs/dev")
            groups = {r["group"] for r in m["resources"]}
            self.assertEqual(groups, {"envs/prod", "envs/dev"})

    def test_roles_carry_their_own_environment_group_too(self):
        with tempfile.TemporaryDirectory() as td:
            m = scan_iac(self._two_env_repo(td))
            roles = {r["id"]: r for r in m["roles"]}
            self.assertEqual(roles["tf:envs/prod::aws_iam_role.app"]["group"], "envs/prod")
            self.assertEqual(roles["tf:envs/dev::aws_iam_role.app"]["group"], "envs/dev")


class TestMultiEnvRepoFixture(unittest.TestCase):
    """The on-disk QA surface for environment visibility (tests/fixtures/
    multienvrepo): envs/prod/main.tf + envs/dev/main.tf, same resource names,
    one role each, different grants -- rendered by codemap and driven through
    the browser in qa/map-qa.mjs as the fourth QA surface. Pinned here at the
    model level too so a parser regression shows up in the fast test suite,
    not only in the (optional, playwright-dependent) QA pass."""

    def test_two_environments_two_groups(self):
        m = scan_iac(FIX_MULTIENV)
        groups = {r["group"] for r in m["resources"]}
        self.assertEqual(groups, {"envs/prod", "envs/dev"})

    def test_same_resource_names_both_envs(self):
        m = scan_iac(FIX_MULTIENV)
        names_by_group = {}
        for r in m["resources"]:
            names_by_group.setdefault(r["group"], set()).add(r["name"])
        self.assertEqual(names_by_group["envs/prod"], names_by_group["envs/dev"])
        self.assertEqual(names_by_group["envs/prod"], {"app", "data", "app_role"})

    def test_one_role_each_with_different_grants(self):
        m = scan_iac(FIX_MULTIENV)
        roles = {r["id"]: r for r in m["roles"]}
        self.assertEqual(len(roles), 2)
        prod = roles["tf:envs/prod::aws_iam_role.app_role"]
        dev = roles["tf:envs/dev::aws_iam_role.app_role"]
        self.assertEqual(prod["group"], "envs/prod")
        self.assertEqual(dev["group"], "envs/dev")
        prod_actions = sorted(prod["grants"][0]["actions"])
        dev_actions = sorted(dev["grants"][0]["actions"])
        self.assertEqual(prod_actions, ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem"])
        self.assertEqual(dev_actions, ["dynamodb:GetItem"])
        self.assertNotEqual(prod_actions, dev_actions)

    def test_no_cross_environment_connections(self):
        m = scan_iac(FIX_MULTIENV)
        self.assertTrue(m["connections"])
        for c in m["connections"]:
            env = "prod" if "prod" in c["src"] else "dev"
            self.assertTrue(c["src"].startswith(f"tf:envs/{env}::"), c)
            self.assertTrue(c["dst"].startswith(f"tf:envs/{env}::"), c)


if __name__ == "__main__":
    unittest.main()
