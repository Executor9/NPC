import logging
from decimal import Decimal
from typing import Dict, List

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory, CandlesConfig
from hummingbot.connector.connector_base import ConnectorBase


class PMMRiskVolatilityTrend(ScriptStrategyBase):
    trading_pair = "ETH-USDT"
    exchange = "binance_paper_trade"
    price_source = PriceType.MidPrice
    order_refresh_time = 15
    order_amount = 0.01

    bid_spread_scalar = 100
    ask_spread_scalar = 50
    max_shift_spread = 0.0005
    trend_scalar = -1
    inventory_skew_strength = 0.5

    candle_exchange = "binance"
    candles_interval = "1m"
    candles_length = 30
    max_records = 1000

    bid_spread = 0.0001
    ask_spread = 0.0001
    reference_price = Decimal("1.0")
    create_timestamp = 0

    candles = CandlesFactory.get_candle(CandlesConfig(
        connector=candle_exchange,
        trading_pair=trading_pair,
        interval=candles_interval,
        max_records=max_records
    ))

    markets = {exchange: {trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.candles.start()

    def on_stop(self):
        self.candles.stop()

    def on_tick(self):
        if self.current_timestamp >= self.create_timestamp:
            self.cancel_all_orders()
            self.update_indicators()
            proposal = self.create_proposal()
            adjusted = self.adjust_proposal_to_budget(proposal)
            self.place_orders(adjusted)
            self.create_timestamp = self.current_timestamp + self.order_refresh_time

    def update_indicators(self):
        df = self.candles.candles_df
        df.ta.natr(length=self.candles_length, append=True)
        df.ta.rsi(length=self.candles_length, append=True)

        natr = df[f"NATR_{self.candles_length}"].iloc[-1]
        rsi = df[f"RSI_{self.candles_length}"].iloc[-1]

        self.bid_spread = natr * self.bid_spread_scalar
        self.ask_spread = natr * self.ask_spread_scalar

        raw_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, self.price_source)
        price_multiplier = ((rsi - 50) / 50) * self.max_shift_spread * self.trend_scalar
        shifted_price = raw_price * Decimal(str(1 + price_multiplier))

        base = self.connectors[self.exchange].get_balance(self.trading_pair.split("-")[0])
        quote = self.connectors[self.exchange].get_balance(self.trading_pair.split("-")[1])
        total_value = base * raw_price + quote
        inventory_ratio = (base * raw_price) / total_value if total_value > 0 else Decimal("0.5")
        skew = (Decimal("0.5") - inventory_ratio) * Decimal(self.inventory_skew_strength)

        self.reference_price = shifted_price * (Decimal("1.0") + skew)

        
        self.log_with_clock(logging.INFO, f"🌪️ NATR: {natr:.4f} → Bid: {self.bid_spread*10000:.1f}bps, Ask: {self.ask_spread*10000:.1f}bps")
        self.log_with_clock(logging.INFO, f"📈 RSI: {rsi:.2f} → Price shift: {price_multiplier * 10000:.2f}bps")

        if skew > 0:
            self.log_with_clock(logging.INFO, "🧠 Risk: Inventory heavy in USDT → Favoring BUYs")
        elif skew < 0:
            self.log_with_clock(logging.INFO, "🧠 Risk: Inventory heavy in ETH → Favoring SELLs")
        else:
            self.log_with_clock(logging.INFO, "🧠 Risk: Inventory balanced")

    def create_proposal(self) -> List[OrderCandidate]:
        best_bid = self.connectors[self.exchange].get_price(self.trading_pair, False)
        best_ask = self.connectors[self.exchange].get_price(self.trading_pair, True)

        buy_price = min(self.reference_price * Decimal(1 - self.bid_spread), best_bid)
        sell_price = max(self.reference_price * Decimal(1 + self.ask_spread), best_ask)

        self.log_with_clock(logging.INFO, f"📌 Quote: Buy @ {buy_price:.2f}, Sell @ {sell_price:.2f}")
        return [
            OrderCandidate(self.trading_pair, True, OrderType.LIMIT, TradeType.BUY, Decimal(self.order_amount), buy_price),
            OrderCandidate(self.trading_pair, True, OrderType.LIMIT, TradeType.SELL, Decimal(self.order_amount), sell_price)
        ]

    def adjust_proposal_to_budget(self, proposal: List[OrderCandidate]) -> List[OrderCandidate]:
        return self.connectors[self.exchange].budget_checker.adjust_candidates(proposal, all_or_none=True)

    def place_orders(self, orders: List[OrderCandidate]):
        for order in orders:
            if order.order_side == TradeType.BUY:
                self.buy(self.exchange, self.trading_pair, order.amount, order.order_type, order.price)
            else:
                self.sell(self.exchange, self.trading_pair, order.amount, order.order_type, order.price)
        self.log_with_clock(logging.INFO, f"📨 Placed {len(orders)} orders.")

    def cancel_all_orders(self):
        active = self.get_active_orders(self.exchange)
        for order in active:
            self.cancel(self.exchange, order.trading_pair, order.client_order_id)
        if active:
            self.log_with_clock(logging.INFO, f"❌ Cancelled {len(active)} existing orders.")

    def did_fill_order(self, event: OrderFilledEvent):
        msg = f"✅ Filled {event.trade_type.name} {event.amount:.4f} {event.trading_pair} @ {event.price:.2f}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

    def format_status(self) -> str:
        lines = []
        lines.append("\n====== Market Maker Status ======\n")
        lines.append(f"Ref Price: {self.reference_price:.2f}")
        lines.append(f"Bid Spread (bps): {self.bid_spread * 10000:.1f}")
        lines.append(f"Ask Spread (bps): {self.ask_spread * 10000:.1f}")
        lines.append(f"Active Orders: {len(self.get_active_orders(self.exchange))}")
        return "\n".join(lines)
