import requests
import singer
import time

from singer import metrics

session = requests.Session()
logger = singer.get_logger()

# see https://app.asana.com/0/1189361572142836/1194396938962481/f and 
# https://simondata.atlassian.net/browse/DI-259?atlOrigin=eyJpIjoiOTJhM2FkNzVjODEzNDIwYjk2Y2JlOTA4YjQ4ZWRlY2QiLCJwIjoiaiJ9
CUSTOM_HEADERS = {'X-SGUID': '3459112'}


def authed_get(tap_stream_id, url, config, params=None):
    headers = {"Authorization": "Bearer %s" % config['api_key']}
    headers.extend(CUSTOM_HEADERS)
    with metrics.http_request_timer(tap_stream_id) as timer:
        resp = session.request(method='get', url=url, params=params, headers=headers)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code
        return resp


def end_of_records_check(r):
    empty_message = "No more pages"
    if r.status_code == 404 and r.json().get(
            'errors', [{}])[0].get('message') == empty_message:
        return True
    if r.json().get('recipient_count') == 0:
        return True
    else:
        return False


def retry_get(tap_stream_id, url, config, params=None):
    """Wrap certain streams in a retry wrapper for frequent 500s"""
    retries = 20
    delay = 120
    backoff = 1.5
    attempt = 1
    while retries >= attempt:
        r = authed_get(tap_stream_id, url, config, params)
        if r.status_code >= 500:
            logger.info(f'Got a status code of {r.status_code}, attempt '
                        f'{attempt} of {retries}. Backing off for {delay} '
                        f'seconds')
            time.sleep(delay)
            delay *= backoff
            attempt += 1
        else:
            return r
    logger.error(f'Status code of latest attempt: {r.status_code}')
    logger.error(f'Latest attempt response {r.content}')
    raise ValueError(f'Failed {retries} times trying to hit endpoint {url}')
