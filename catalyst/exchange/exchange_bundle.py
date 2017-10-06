import os
from datetime import timedelta

import numpy as np
import pandas as pd
from logbook import Logger

from catalyst import get_calendar
from catalyst.data.minute_bars import BcolzMinuteOverlappingData, \
    BcolzMinuteBarWriter, BcolzMinuteBarReader
from catalyst.data.us_equity_pricing import BcolzDailyBarWriter, \
    BcolzDailyBarReader
from catalyst.exchange.bundle_utils import fetch_candles_chunk
from catalyst.exchange.exchange_utils import get_exchange_folder
from catalyst.exchange.init_utils import get_exchange
from catalyst.utils.cli import maybe_show_progress
from catalyst.utils.paths import ensure_directory


def _cachpath(symbol, type_):
    return '-'.join([symbol, type_])


BUNDLE_NAME_TEMPLATE = '{root}/{frequency}_bundle'
log = Logger('exchange_bundle')


class ExchangeBundle:
    def __init__(self, exchange_name, data_frequency, include_symbols=None,
                 exclude_symbols=None, start=None, end=None,
                 show_progress=True, environ=os.environ):
        self.exchange = get_exchange(exchange_name)
        self.data_frequency = data_frequency
        self.assets = self.get_assets(include_symbols, exclude_symbols)
        self.start, self.end = self.get_adj_dates(start, end)
        self.environ = environ
        self.show_progress = show_progress
        self.minutes_per_day = 1440
        self.default_ohlc_ratio = 1000000
        self._writer = None
        self._reader = None

    def get_assets(self, include_symbols, exclude_symbols):
        # TODO: filter exclude symbols assets
        if include_symbols is not None:
            include_symbols_list = include_symbols.split(',')

            return self.exchange.get_assets(include_symbols_list)

        else:
            return self.exchange.get_assets()

    def get_adj_dates(self, start, end):
        now = pd.Timestamp.utcnow()
        if end > now:
            log.info('adjusting the end date to now {}'.format(now))
            end = now

        earliest_trade = None
        for asset in self.assets:
            if earliest_trade is None or earliest_trade > asset.start_date:
                earliest_trade = asset.start_date

        if earliest_trade > start:
            log.info(
                'adjusting start date to earliest trade date found {}'.format(
                    earliest_trade
                ))
            start = earliest_trade

        if start >= end:
            raise ValueError('start date cannot be after end date')

        return start, end

    @property
    def reader(self):
        if self._reader is not None:
            return self._reader

        root = get_exchange_folder(self.exchange.name)
        input_dir = BUNDLE_NAME_TEMPLATE.format(
            root=root,
            frequency=self.data_frequency
        )

        if self.data_frequency == 'minute':
            try:
                self._reader = BcolzMinuteBarReader(input_dir)
            except IOError:
                log.debug('no reader data found in {}'.format(input_dir))

        elif self.data_frequency == 'daily':
            try:
                self._reader = BcolzDailyBarReader(input_dir)
            except IOError:
                log.debug('no reader data found in {}'.format(input_dir))
        else:
            raise ValueError(
                'invalid frequency {}'.format(self.data_frequency)
            )

        return self._reader

    @property
    def writer(self):
        if self._writer is not None:
            return self._writer

        open_calendar = get_calendar('OPEN')

        root = get_exchange_folder(self.exchange.name)
        output_dir = BUNDLE_NAME_TEMPLATE.format(
            root=root,
            frequency=self.data_frequency
        )
        ensure_directory(output_dir)

        if self.data_frequency == 'minute':
            if len(os.listdir(output_dir)) > 0:
                self._writer = BcolzMinuteBarWriter.open(output_dir, self.end)
            else:
                self._writer = BcolzMinuteBarWriter(
                    rootdir=output_dir,
                    calendar=open_calendar,
                    minutes_per_day=self.minutes_per_day,
                    start_session=self.start,
                    end_session=self.end,
                    write_metadata=True,
                    default_ohlc_ratio=self.default_ohlc_ratio
                )
        elif self.data_frequency == 'daily':
            if len(os.listdir(output_dir)) > 0:
                self._writer = BcolzDailyBarWriter.open(output_dir, self.end)
            else:
                self._writer = BcolzDailyBarWriter(
                    filename=output_dir,
                    calendar=open_calendar,
                    start_session=self.start,
                    end_session=self.end
                )
        else:
            raise ValueError(
                'invalid frequency {}'.format(self.data_frequency)
            )

        return self._writer

    def check_data_exists(self, assets, start, end):
        has_data = True
        for asset in assets:
            if has_data and self.reader is not None:
                try:
                    start_close = self.reader.get_value(
                        asset.sid, start, 'close')

                    if np.isnan(start_close):
                        has_data = False

                    else:
                        end_close = self.reader.get_value(
                            asset.sid, end, 'close')

                        if np.isnan(end_close):
                            has_data = False

                except Exception as e:
                    has_data = False

            else:
                has_data = False

        return has_data

    def ingest(self):
        symbols = []
        log.debug(
            'ingesting trading pairs {symbols} on exchange {exchange} '
            'from {start} to {end}'.format(
                symbols=symbols,
                exchange=self.exchange.name,
                start=self.start,
                end=self.end
            )
        )

        delta = self.end - self.start
        if self.data_frequency == 'minute':
            delta_periods = delta.total_seconds() / 60
            frequency = '1m'

        elif self.data_frequency == 'daily':
            delta_periods = delta.total_seconds() / 60 / 60 / 24
            frequency = '1d'

        else:
            raise ValueError('frequency not supported')

        if delta_periods > self.exchange.num_candles_limit:
            bar_count = self.exchange.num_candles_limit

            chunks = []
            last_chunk_date = self.end.floor('1 min')
            while last_chunk_date > self.start + timedelta(minutes=bar_count):
                # TODO: account for the partial last bar
                chunk = dict(end=last_chunk_date, bar_count=bar_count)
                chunks.append(chunk)

                # TODO: base on frequency
                last_chunk_date = \
                    last_chunk_date - timedelta(minutes=(bar_count + 1))

            chunks.reverse()

        else:
            chunks = [dict(end=self.end, bar_count=delta_periods)]

        with maybe_show_progress(
                chunks,
                self.show_progress,
                label='Fetching {exchange} {frequency} candles: '.format(
                    exchange=self.exchange.name,
                    frequency=self.data_frequency
                )) as it:

            previous_candle = dict()
            for chunk in it:
                chunk_end = chunk['end']
                chunk_start = chunk_end - timedelta(minutes=chunk['bar_count'])

                chunk_assets = []
                for asset in self.assets:
                    if asset.start_date <= chunk_end:
                        chunk_assets.append(asset)

                if self.check_data_exists(
                        chunk_assets, chunk_start, chunk_end):
                    log.debug('the data chunk already exists')
                    continue

                # TODO: ensure correct behavior for assets starting in the chunk
                candles = fetch_candles_chunk(
                    exchange=self.exchange,
                    assets=chunk_assets,
                    data_frequency=frequency,
                    end_dt=chunk_end,
                    bar_count=chunk['bar_count']
                )
                log.debug(
                    'requests counter {}'.format(self.exchange.request_cpt))

                num_candles = 0
                data = []
                for asset in candles:
                    asset_candles = candles[asset]
                    if not asset_candles:
                        log.debug(
                            'no data: {symbols} on {exchange}, date {end}'.format(
                                symbols=chunk_assets,
                                exchange=self.exchange.name,
                                end=chunk_end
                            )
                        )
                        continue

                    all_dates = []
                    all_candles = []
                    date = chunk_start
                    while date <= chunk_end:

                        previous = previous_candle[asset] \
                            if asset in previous_candle else None

                        candle = next((candle for candle in asset_candles \
                                       if candle['last_traded'] == date),
                                      previous)

                        if candle is not None:
                            all_dates.append(date)
                            all_candles.append(candle)

                            previous_candle[asset] = candle

                        date += timedelta(minutes=1)

                    df = pd.DataFrame(all_candles, index=all_dates)
                    if not df.empty:
                        df.sort_index(inplace=True)

                        sid = asset.sid
                        num_candles += len(df.values)

                        data.append((sid, df))

                try:
                    log.debug(
                        'writing {num_candles} candles from {start} to {end}'.format(
                            num_candles=num_candles,
                            start=chunk_start,
                            end=chunk_end
                        )
                    )

                    for pair in data:
                        log.debug('data for sid {}\n{}\n{}'.format(
                            pair[0], pair[1].head(2), pair[1].tail(2)))

                    self.writer.write(
                        data=data,
                        show_progress=False,
                        invalid_data_behavior='raise'
                    )
                except BcolzMinuteOverlappingData as e:
                    log.warn('chunk already exists {}: {}'.format(chunk, e))
