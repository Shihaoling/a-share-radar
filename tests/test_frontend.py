"""Browser display invariants, runnable in Node or macOS JavaScriptCore.

No network or page automation. DOM doubles check how quotes, taxonomy, provider
valuation and article evidence are selected; visual browser QA remains separate.
"""
from pathlib import Path
import json
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FrontendContracts(unittest.TestCase):
    def test_display_invariants(self):
        app = (ROOT / 'web/app.js').read_text()
        harness = (ROOT / 'tests/frontend_logic.js').read_text()
        expression = f'runFrontendChecks({json.dumps(app, ensure_ascii=False)})'
        node = shutil.which('node')
        jsc = shutil.which('osascript')
        if node:
            command = [node]
            script = harness + '\nconsole.log(JSON.stringify(' + expression + '));\n'
        elif jsc:
            command = [jsc, '-l', 'JavaScript']
            script = harness + '\nJSON.stringify(' + expression + ');\n'
        else:
            self.skipTest('Node or macOS JavaScriptCore is required for frontend contract checks')
        result = subprocess.run(command, input=script, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertGreaterEqual(payload['passed'], 40)
        self.assertEqual(payload['failures'], [], json.dumps(payload['failures'], ensure_ascii=False))

    @unittest.skipUnless(shutil.which('node'), 'Node required for asynchronous polling checks')
    def test_refresh_recovery(self):
        script = (ROOT / 'tests/frontend_logic.js').read_text() + """
const fs = require('node:fs');
runRefreshChecks(fs.readFileSync('web/app.js','utf8'))
    .catch(error => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node')], cwd=ROOT, input=script, text=True,
                                capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
