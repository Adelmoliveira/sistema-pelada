"""Run #249 with ONLY the #250 fixture setUp loaded in memory; no merge/checkouts.

PYTHONPATH=tests:. .venv/bin/python tests/run_pr249_combined_validation.py
All tests retain the repository's disposable database fixtures.
"""
import subprocess
import sys
import types
import unittest
from pathlib import Path

FIXTURE_COMMIT='f80fef0cc10276ec5c11744ed6e3546f850f8400'


def main():
    import test_sports_installment_eligibility as original
    source=subprocess.check_output(['git','show',FIXTURE_COMMIT+':tests/test_sports_installment_eligibility.py'],text=True)
    fixed=types.ModuleType('fixed_eligibility_fixture')
    fixed.__file__=str(Path('tests/test_sports_installment_eligibility.py').resolve())
    exec(compile(source,fixed.__file__,'exec'),fixed.__dict__)
    original.SportsEligibilityTest.setUp=fixed.SportsEligibilityTest.setUp
    print('Fixture setUp from #250 commit '+FIXTURE_COMMIT+' (in memory only)',flush=True)
    result=unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.discover('tests'))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    sys.exit(main())
