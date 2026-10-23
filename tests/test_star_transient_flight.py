"""Bounded retry for Star 200 Flight responses that contain no hospital data."""
import pathlib, sys, unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import collect_star as star

VALID='1:{"description":"success","count":"1","details":[{"hospitalName":"Clinic"}],"responseCode":"200"}'
EMPTY='0:[]\n1:["$","div",null,{"children":"temporary"}]'
ERROR='1:{"errorDesc":"temporarily unavailable","responseCode":"500"}'

class RetryFlight(unittest.TestCase):
    def args(self):
        return star.base.make_args(state='Maharashtra',category='agreed',page=132)
    def test_transient_frame_retries_then_succeeds(self):
        with patch.object(star.base,'fetch',side_effect=[(200,'text/x-component',EMPTY),(200,'text/x-component',VALID)]) as f,patch.object(star.time,'sleep') as delay:
            records,count,pages=star.source_action('a'*40,self.args(),10)
        self.assertEqual((len(records),count,pages),(1,1,1))
        self.assertEqual(f.call_count,2)
        delay.assert_called_once_with(1)
    def test_repeated_missing_frame_fails_closed_after_three(self):
        with patch.object(star.base,'fetch',return_value=(200,'text/x-component',EMPTY)) as f,patch.object(star.time,'sleep') as delay:
            with self.assertRaisesRegex(star.SourceError,'after 3 bounded attempts'):
                star.source_action('a'*40,self.args(),10)
        self.assertEqual(f.call_count,3)
        self.assertEqual(delay.call_count,2)
    def test_explicit_backend_error_is_not_treated_as_empty(self):
        with patch.object(star.base,'fetch',return_value=(200,'text/x-component',ERROR)) as f,patch.object(star.time,'sleep') as delay:
            with self.assertRaises(star.SourceError):
                star.source_action('a'*40,self.args(),10)
        self.assertEqual(f.call_count,1)
        delay.assert_not_called()
    def test_access_denied_not_retried(self):
        with patch.object(star.base,'fetch',return_value=(403,'text/x-component','')) as f,patch.object(star.time,'sleep') as delay:
            with self.assertRaisesRegex(star.SourceError,'HTTP 403'):
                star.source_action('a'*40,self.args(),10)
        self.assertEqual(f.call_count,1)
        delay.assert_not_called()

if __name__=='__main__':unittest.main()

