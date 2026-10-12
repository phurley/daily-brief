"""Exercise the calendar collector's actual retry paths without network calls."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

spec = importlib.util.spec_from_file_location('calendar_collector', Path(__file__).with_name('update_calendar.py'))
calendar = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = calendar
spec.loader.exec_module(calendar)


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({'choices': [{'message': {'content': '{"items": []}'}}]}).encode()


class CalendarRetryTests(unittest.TestCase):
    def test_transient_failures_retry_without_shadowing_datetime_time(self):
        errors = [URLError('temporary outage'),
                  HTTPError('https://example.test', 429, 'rate limited', {}, io.BytesIO(b'try later'))]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.object(calendar.urllib.request, 'urlopen', side_effect=[error, Response()]) as request, \
                    patch.object(calendar.clock_time, 'sleep') as sleep:
                result = calendar._llm_request([], {}, 'test-model', 'test-key')
                self.assertEqual(result, {'items': []})
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(1.0)

    def test_exhausted_network_retries_raise_supported_error(self):
        with patch.object(calendar.urllib.request, 'urlopen', side_effect=URLError('unavailable')), \
                patch.object(calendar.clock_time, 'sleep') as sleep:
            with self.assertRaises(RuntimeError):
                calendar._llm_request([], {}, 'test-model', 'test-key')
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0, 4.0])


if __name__ == '__main__':
    unittest.main()
