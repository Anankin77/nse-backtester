"""Indian equity delivery (CNC) trading costs, as a fraction of trade value.

Approximate NSE rates; verify on Zerodha's brokerage calculator before relying
on them, since exchanges and SEBI revise these.

  Brokerage (Zerodha delivery) : 0
  STT                          : 0.1% on buy and on sell
  Exchange transaction charge  : ~0.00297% (NSE)
  SEBI fee                     : 0.0001%
  Stamp duty                   : 0.015% on buy only
  GST                          : 18% on (brokerage + exchange + SEBI)

Not modelled: the flat DP charge (~Rs 15-16 per scrip per sell day), which matters
for small orders. Slippage (you rarely get the exact price) is added separately.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    stt: float = 0.001
    exchange: float = 0.0000297
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
