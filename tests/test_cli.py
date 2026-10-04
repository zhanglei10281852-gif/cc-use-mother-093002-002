import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from resource_review.cli import main

ROOT = Path(__file__).parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = str(Path(self.tmp.name) / "store.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, expect=0):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--store", self.store, *argv])
        self.assertEqual(code, expect, buffer.getvalue())
        output = buffer.getvalue()
        return json.loads(output) if output.strip() else None

    def test_full_offline_workflow(self):
        decl = self.run_cli(
            "declare-source", "--origin", "生成", "--tool", "某生成工具", "--id", "SRC-1"
        )
        self.assertEqual(decl["origin"], "generated")

        version = self.run_cli(
            "submit",
            "--kind", "讲义单元",
            "--entity", "E-1",
            "--name", "第一课",
            "--declaration", "SRC-1",
            "--content", "例句：泼水节大家互相泼水。",
            "--key", "sub-1",
        )
        self.assertEqual(version["version_id"], "E-1-v1")
        # 重复提交同一幂等键：返回同一版本，不产生新版本
        again = self.run_cli(
            "submit",
            "--kind", "讲义单元",
            "--entity", "E-1",
            "--declaration", "SRC-1",
            "--content", "例句：泼水节大家互相泼水。",
            "--key", "sub-1",
        )
        self.assertEqual(again["version_id"], "E-1-v1")

        self.run_cli("register-reviewer", "--name", "王文化", "--qual", "文化", "--id", "REV-1")
        self.run_cli("assign", "--version", "E-1-v1", "--dimension", "文化", "--reviewer", "REV-1")
        self.run_cli(
            "opinion",
            "--version", "E-1-v1",
            "--dimension", "文化",
            "--reviewer", "REV-1",
            "--risk", "低",
            "--verdict", "通过",
        )
        conclusion = self.run_cli("conclude", "--version", "E-1-v1")
        self.assertEqual(conclusion["result"], "passed")
        self.assertEqual(conclusion["status"], "final")

        token = self.run_cli("publish", "--version", "E-1-v1", "--key", "pub-1")
        self.assertEqual(token["digest"], version["digest"])

        course = self.run_cli("course-publish", "--course", "C-1", "--entry", "E-1:E-1-v1")
        self.assertEqual(course["entries"][0]["digest"], version["digest"])

        revoked = self.run_cli("revoke", "--entity", "E-1", "--reason", "习俗解释有误")
        self.assertEqual(len(revoked["dispositions"]), 1)
        disposition_id = revoked["dispositions"][0]["disposition_id"]

        pending = self.run_cli("pending")
        self.assertEqual(
            [d["disposition_id"] for d in pending["pending_dispositions"]], [disposition_id]
        )

        done = self.run_cli("dispose", "--disposition", disposition_id, "--note", "已隔离")
        self.assertEqual(done["status"], "done")

        trace = self.run_cli("trace", "E-1")
        self.assertEqual(trace["origin_chain"][0]["declaration"]["tool_name"], "某生成工具")
        self.assertEqual(len(trace["versions"][0]["opinions"]), 1)
        self.assertEqual(trace["propagation"][0]["course_id"], "C-1")
        self.assertEqual(trace["revocations"][0]["dispositions"][0]["status"], "done")

    def test_domain_error_returns_exit_code_2(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--store", self.store, "trace", "NOPE"])
        self.assertEqual(code, 2)

    def test_smoke_script_still_works(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "run_cli.py")],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entity"], "智能教学资源风险审校")
        self.assertEqual(payload["record_state"], "已登记")


if __name__ == "__main__":
    unittest.main()
