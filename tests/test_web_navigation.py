import pathlib,shutil,subprocess,unittest

class TestWebNavigation(unittest.TestCase):
 @unittest.skipUnless(shutil.which("node"), "Node.js required for frontend VM test")
 def test_late_responses_and_overlapping_refresh(self):
  root=pathlib.Path(__file__).resolve().parents[1]
  r=subprocess.run(["node",str(root/"tests/web_navigation_probe.js"),str(root/"web/index.html")],capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr)
  for expected in ["ABA_STALE_WRITE=false","OLD_GET_ABORTED=true","CROSS_PAGE_STALE_WRITE=false","OVERLAPPING_RENDER_CALLS=1","BACKGROUND_CANCELLED=false","POST_CANCELLED=false","UNEXPECTED_ERRORS=0"]:
   self.assertIn(expected,r.stdout)
