"""Free customer paths through the promoted, mocked cache/start controls."""

import unittest

import test_manual_selection
import test_optimizer_start_cache


class CustomerCacheControlsTests(unittest.TestCase):
    def fixture(self, manual=False):
        f = (
            test_manual_selection.SelectionTests()
            if manual
            else test_optimizer_start_cache.OnCacheTests()
        )
        f.setUp()

        def close():
            try:
                f.tearDown()
            finally:
                f.doCleanups()

        self.addCleanup(close)
        return f

    def test_free_customer_can_start_selected_model_in_manual(self):
        f = self.fixture(manual=True)
        f.stopped()
        f.admit()
        result = f.run_worker()
        self.assertEqual(result['selectionResult']['status'], 'completed')
        self.assertEqual(result['selectionResult']['model'], 'b')
        self.assertEqual(f.o.state['mode'], 'observe')

    def test_free_on_uses_existing_guarded_cleanup_and_start(self):
        f = self.fixture()
        result = f.execute()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((f.purge_count, f.start_count), (1, 1))


if __name__ == '__main__':
    unittest.main()
