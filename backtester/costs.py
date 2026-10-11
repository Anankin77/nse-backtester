"""Indian equity delivery (CNC) trading costs, as a fraction of trade value.

Rates from zerodha.com/charges (checked Oct 2026). Exchanges and SEBI revise
these, so re-check occasionally.

  Brokerage (Zerodha delivery) : 0
  STT                          : 0.1% on buy and on sell
  NSE transaction charge       : 0.00307%
  SEBI fee                     : Rs 10 per crore (0.0001%)
  Stamp duty                   : 0.015% on buy only
  GST                          : 18% on (brokerage + exchange + SEBI)

Not modelled: the flat DP charge (Rs 15.34 per scrip per sell day), which matters
for small orders: on a Rs 5,000 sell it adds ~0.3%. Slippage (you rarely get the
exact price) is added separately.
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

    @property
    def buy(self) -> float:
        return self.stt + self.exchange + self.sebi + self.stamp_buy + self.gst * (self.exchange + self.sebi) + self.slippage

    @property
    def sell(self) -> float:
        return self.stt + self.exchange + self.sebi + self.gst * (self.exchange + self.sebi) + self.slippage


ZERO = CostModel(0, 0, 0, 0, 0, 0)
DELIVERY = CostModel()
