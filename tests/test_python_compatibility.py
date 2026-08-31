from pathlib import Path
import unittest


class SupportedPythonSyntaxTests(unittest.TestCase):
    def test_xiaohongshu_module_compiles(self):
        source_path = (
            Path(__file__).parents[1]
            / "uploader"
            / "xiaohongshu_uploader"
            / "main.py"
        )
        source = source_path.read_text(encoding="utf-8")

        try:
            compile(source, str(source_path), "exec")
        except SyntaxError as exc:
            self.fail(f"xiaohongshu module must compile on supported Python: {exc}")


if __name__ == "__main__":
    unittest.main()
