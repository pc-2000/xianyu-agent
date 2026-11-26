"""Isolate session recovery from browser startup and account side effects."""
import ast
from pathlib import Path
import unittest
from unittest.mock import Mock


source = Path(__file__).resolve().parents[1] / 'utils' / 'xianyu_slider_stealth.py'
tree = ast.parse(source.read_text(encoding='utf-8-sig'))
original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuSliderStealth')
methods = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in {
    '_browser_session_alive', '_recover_browser_session', 'solve_slider'}]
isolated = ast.Module(body=[ast.ClassDef(name='Slider', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
namespace = {'logger': Mock()}
exec(compile(ast.fix_missing_locations(isolated), str(source), 'exec'), namespace)


class RecoveryTests(unittest.TestCase):
    def slider(self):
        slider = namespace['Slider']()
        slider.pure_user_id = 'test'
        slider.browser = Mock()
        slider.browser.is_connected.return_value = True
        slider.page = Mock()
        slider.page.is_closed.return_value = False
        slider.page.url = 'https://example.test/verification'
        return slider

    def test_closed_page_is_not_alive(self):
        slider = self.slider()
        slider.page.is_closed.return_value = True
        self.assertFalse(slider._browser_session_alive())

    def test_recovery_discards_frame_and_reopens_url(self):
        slider = self.slider()
        slider._detected_slider_frame = object()
        slider._cleanup_on_init_failure = Mock()
        page = Mock()
        page.is_closed.return_value = False
        browser = Mock()
        browser.is_connected.return_value = True
        def initialize():
            slider.page, slider.browser = page, browser
        slider.init_browser = Mock(side_effect=initialize)
        self.assertTrue(slider._recover_browser_session('https://example.test/verification'))
        self.assertFalse(hasattr(slider, '_detected_slider_frame'))
        page.goto.assert_called_once_with('https://example.test/verification', wait_until='domcontentloaded', timeout=30000)

    def test_failed_recovery_stops_before_element_lookup(self):
        slider = self.slider()
        slider.browser.is_connected.return_value = False
        slider._recover_browser_session = Mock(return_value=False)
        slider.find_slider_elements = Mock()
        self.assertFalse(slider.solve_slider())
        slider.find_slider_elements.assert_not_called()
        slider._recover_browser_session.assert_called_once_with(slider.page.url)

    def test_missing_url_does_not_start_browser(self):
        slider = self.slider()
        slider.init_browser = Mock()
        self.assertFalse(slider._recover_browser_session(None))
        slider.init_browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
