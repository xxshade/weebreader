"""Browser selection and completion criteria shared by desktop checks."""
import argparse

BROWSERS = ('chrome', 'firefox')


def add_browser_argument(parser):
    parser.add_argument('--browser', choices=('both', *BROWSERS), default='both',
                        help='Desktop target to execute (default: both browsers)')


def selected_browsers(args):
    return BROWSERS if args.browser == 'both' else (args.browser,)


def baseline_passed(result, expected):
    return expected > 0 and result.wasSuccessful() and not result.skipped and result.testsRun == expected


def mutation_rejected(result, browsers, suffix, signal):
    rejected = {str(test) for test, message in result.failures if signal in message}
    return (bool(browsers) and result.testsRun == len(browsers) and not result.errors
            and not result.skipped and all(
                any(browser.title() + suffix in name for name in rejected) for browser in browsers))


def browser_results_passed(chrome, firefox):
    return chrome == 'success' and firefox == 'success'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Require successful results from both browser jobs')
    parser.add_argument('--chrome-result', required=True)
    parser.add_argument('--firefox-result', required=True)
    args = parser.parse_args()
    if not browser_results_passed(args.chrome_result, args.firefox_result):
        raise SystemExit(f'Browser validation incomplete: Chrome={args.chrome_result}, Firefox={args.firefox_result}')
    print('Chrome and Firefox passed all required browser checks.')
