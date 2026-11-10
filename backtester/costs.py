"""Indian equity delivery (CNC) trading costs, as a fraction of trade value.

Rates from zerodha.com/charges (checked Oct 2026). Exchanges and SEBI revise
these, so re-check occasionally.

  Brokerage (Zerodha delivery) : 0
  STT                          : 0.1% on buy and on sell
  NSE transaction charge       : 0.00307%
  SEBI fee                     : Rs 10 per crore (0.0001%)
  Stamp duty                   : 0.015% on buy only
  GST                          : 18% on (brokerage + exchange + SEBI)

  DP charge                    : Rs 15.34 flat, per scrip per sell day
                                 (Rs 3.5 CDSL + Rs 9.5 Zerodha + 18% GST)

The DP charge is a fixed rupee amount, not a fraction, so it is kept apart from
`buy` / `sell` and subtracted from the proceeds of each sale. It is what makes small
orders expensive: on a Rs 1,000 sell it costs 1.5%, on Rs 1,00,000 only 0.015%.
Slippage (you rarely get the exact price) is added separately.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    stt: float = 0.001
    exchange: float = 0.0000307
    sebi: float = 0.000001
    stamp_buy: float = 0.00015
    gst: float = 0.18
    slippage: float = 0.0005  # 0.05% per side
    dp_charge: float = 15.34  # rupees, once per scrip per day on which you sell it

    @property
    def buy(self) -> float:
        return self.stt + self.exchange + self.sebi + self.stamp_buy + self.gst * (self.exchange + self.sebi) + self.slippage

    @property
    def sell(self) -> float:
        return self.stt + self.exchange + self.sebi + self.gst * (self.exchange + self.sebi) + self.slippage


    def sell_proceeds(self, value: float) -> float:
        """Cash received for selling `value` rupees of ONE scrip in one day, after all charges."""
        return value * (1 - self.sell) - self.dp_charge


ZERO = CostModel(0, 0, 0, 0, 0, 0, 0)
DELIVERY = CostModel()
