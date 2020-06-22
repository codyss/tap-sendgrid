import pendulum
import singer
from simplejson.scanner import JSONDecodeError

from .streams import IDS
from .http import end_of_records_check, retry_get
from .utils import (
    trimmed_records, trim_members_all, get_results_from_payload,
    safe_update_dict,
    write_records, get_tap_stream_tuple, find_old_list_count,
    get_added_properties
)

logger = singer.get_logger()


class Syncer(object):

    def __init__(self, ctx):
        self.ctx = ctx

    def sync(self):
        self.sync_alls()
        self.sync_incrementals()
        self.ctx.write_state()

    def sync_incrementals(self):
        for cat_entry in self.ctx.selected_catalog:
            stream = get_tap_stream_tuple(cat_entry.tap_stream_id)
            if stream.bookmark:
                getattr(self, 'sync_%s' % stream.bookmark[1])(
                    stream, cat_entry.schema)

                self.ctx.write_state()

    def sync_timestamp(self, stream, schema):
        """
        Searches using created and updated ates as created doesn't also
        impact updated
        """
        start = self.ctx.update_start_date_bookmark(stream.bookmark)

        for day in self.discrete_days_since_start(start):
            for search_term in ['created_at', 'updated_at']:
                params = {
                    search_term: day.int_timestamp
                }
                logger.info("Looking for contacts %s on %s" % (
                    search_term, day.to_date_string()))
                self.write_paged_records(stream, schema, params=params)
            self.ctx.set_bookmark(stream.bookmark, self.ctx.now_date_str())

    def write_paged_records(self, stream, schema,
                            params=None,
                            url_key=None,
                            added_properties=None):

        for res in self.get_using_paged(stream, add_params=params, url_key=url_key):
            try:
                results = res.get('recipients')
            except JSONDecodeError as e:
                logger.info(f'Response: {res}')
                raise e
            if results:
                self.write_records(schema, results, stream,
                                   added_properties=added_properties)

    @staticmethod
    def write_records(schema, results, stream, added_properties=None):
        records = trimmed_records(schema, results, stream, added_properties)
        write_records(stream.tap_stream_id, records)

    def sync_end_time(self, stream, schema):
        start = self.ctx.update_start_date_bookmark(stream.bookmark).int_timestamp
        end = self.ctx.now_seconds

        logger.info('Starting to extract %s from %s to %s' % (
            stream.tap_stream_id, str(start), str(end)))

        for results in self.get_using_offset(stream, start, end):
            self.write_records(schema, results, stream)
            self.ctx.set_bookmark(stream.bookmark, self.ctx.ts_to_dt(end))

    def sync_member_count(self, stream, schema):
        stream_type = trim_members_all(stream.tap_stream_id)

        for list in self.ctx.cache[stream_type]:
            old_list_count = find_old_list_count(
                list['id'],
                self.ctx.update_start_date_bookmark(stream.bookmark))
            if list['member_count'] > old_list_count:
                logger.info('Starting to extract %s as list size now: %s, was: %s' % (
                    stream.tap_stream_id, list['member_count'], old_list_count))

                self.get_and_write_members(list, stream, schema)
            else:
                logger.info('Not syncing %s %s as it is same size as last sync'
                            % (stream_type, list['id']))

    def sync_member_count_limits(self, stream, schema):
        """Grabs the member counts, except by moving by limits"""
        logger.info(f'Starting extract for {stream.tap_stream_id}')

        for results in self.get_members_limits(stream):
            self.write_records(schema, results, stream)

    def sync_alls(self):
        for cat_entry in self.ctx.selected_catalog:
            stream = get_tap_stream_tuple(cat_entry.tap_stream_id)
            if not stream.bookmark:
                logger.info('Extracting all %s' % stream.tap_stream_id)
                for result in self.get_alls(stream):
                    self.write_records(cat_entry.schema, result, stream)
                    self.ctx.update_cache(result, cat_entry.tap_stream_id)

    def get_and_write_members(self, list, stream, schema):
        added_properties = get_added_properties(stream, list['id'])

        if stream.tap_stream_id == IDS.GROUPS_MEMBERS:
            url_key = list['id']
            endpoint = stream.endpoint.format(url_key)
            result = get_results_from_payload(retry_get(
                stream.tap_stream_id, endpoint, self.ctx.config).json())
            self.write_records(schema, result, stream,
                                added_properties=added_properties)

        else:
            self.write_paged_records(stream, schema, url_key=list['id'],
                                     added_properties=added_properties)

        self.ctx.save_member_count_state(list, stream)

    def get_members_limits(self, stream, url_key=None):
        """Grabs all members incremental for a given stream"""
        offset = 0
        limit = 250000
        endpoint = stream.endpoint.format(url_key) if url_key else stream.endpoint

        while True:
            r = retry_get(
                stream.tap_stream_id,
                endpoint,
                self.ctx.config,
                params={
                    'offset': offset,
                    'limit': limit
                }
            )
            try:
                yield r.json()
            except JSONDecodeError:
                logger.error(f'Status code throwing error {r.status_code}')
                logger.error(f'Content for invalid request:\n{r.content}')
                raise ValueError('Error parsing file...')
            if len(r.json()):
                offset += limit
            else:
                break

    def get_alls(self, stream, url_key=None):
        start = pendulum.parse(self.ctx.config['start_date']).int_timestamp
        end = self.ctx.now_seconds

        results = self.get_using_offset(stream, start, end, url_key)
        return results

    def get_using_paged(self, stream, add_params=None, url_key=None):
        page = 1
        page_size = 1000
        endpoint = stream.endpoint.format(url_key) if url_key else stream.endpoint
        page_attempts = 0

        while True:
            params = {
                'page': page,
                'page_size': page_size
            }
            safe_update_dict(params, add_params)
            r = retry_get(stream.tap_stream_id,
                           endpoint,
                           self.ctx.config,
                           params=params)
            try:
                yield r.json()
            except JSONDecodeError:
                page_attempts += 1
                if page_attempts < 3:
                    continue
                else:
                    logger.error(f'Status code throwing error {r.status_code}')
                    logger.error(f'Content for invalid request:\n{r.content}')
                    raise ValueError('Error parsing file...')
            if not end_of_records_check(r):
                page_attempts = 0
                page += 1
            else:
                break

    def get_using_offset(self, stream, start, end, url_key=None):
        offset = 0
        limit = 500
        endpoint = stream.endpoint.format(url_key) if url_key else stream.endpoint

        while True:
            r = get_results_from_payload(retry_get(
                stream.tap_stream_id,
                endpoint,
                self.ctx.config,
                params={
                    'offset': offset,
                    'limit': limit,
                    'start_time': start,
                    'end_time': end
                }
            ).json())
            yield r
            if len(r) >= limit:
                offset += limit
            else:
                break

    def discrete_days_since_start(self, start):
        """
        Return List timestamps each day since start
        """
        search_term = start.start_of('day')
        days_to_search = []
        while search_term <= self.ctx.now:
            days_to_search.append(search_term)
            search_term = search_term.add(days=1)

        return days_to_search
