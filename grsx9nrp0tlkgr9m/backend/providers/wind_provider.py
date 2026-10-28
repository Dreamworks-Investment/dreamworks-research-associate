from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd


CORE_STATEMENT_FIELDS: Dict[str, str] = {
    "tot_oper_rev": "营业总收入",
    "np_belongto_parcomsh": "归属母公司股东的净利润",
    "tot_assets": "资产总计",
    "net_cash_flows_oper_act": "经营活动产生的现金流量净额",
}


@dataclass
class WindReportRow:
    rpt_date: str
    values: Dict[str, Optional[float]]

    def to_dict(self) -> Dict[str, Any]:
        row = {"rpt_date": self.rpt_date}
        row.update(self.values)
        return row


class WindResearchProvider:
    """
    Minimal Wind provider for the MVP.

    Current scope:
    - connect to WindPy
    - fetch one report-date snapshot
    - fetch 5-year core statement data for a stock
    """

    def __init__(self) -> None:
        try:
            from WindPy import w  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "WindPy is not available. Please make sure Wind API is installed "
                "and the current Python environment can import WindPy."
            ) from exc

        self.w = w
        self._ensure_connected()

    def _ensure_connected(self) -> None:
        start_ret = self.w.start()
        error_code = getattr(start_ret, "ErrorCode", -1)
        if error_code != 0:
            raise RuntimeError(f"Wind start failed: {start_ret}")

        if not self.w.isconnected():
            raise RuntimeError("Wind is not connected.")

    def _build_options(self, rpt_date: str, unit: int = 1, rpt_type: int = 1) -> str:
        # rptType=1 means consolidated statements in your current generator output.
        return f"unit={unit};rptDate={rpt_date};rptType={rpt_type};"

    def fetch_report_snapshot(
        self,
        stock_code: str,
        rpt_date: str,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch one report-date snapshot from Wind as a dict.
        """
        selected_fields = fields or list(CORE_STATEMENT_FIELDS.keys())
        field_str = ",".join(selected_fields)
        options = self._build_options(rpt_date=rpt_date)

        err, df = self.w.wss(
            stock_code,
            field_str,
            options,
            usedf=True,
        )

        if err != 0:
            raise RuntimeError(
                f"Wind wss failed for {stock_code} @ {rpt_date}, error={err}"
            )

        if not isinstance(df, pd.DataFrame) or df.empty:
            raise RuntimeError(
                f"Wind returned empty dataframe for {stock_code} @ {rpt_date}"
            )

        first_row = df.iloc[0].to_dict()

        normalized: Dict[str, Any] = {
            "stock_code": stock_code,
            "rpt_date": rpt_date,
        }

        for field in selected_fields:
            value = first_row.get(field.upper())
            if pd.isna(value):
                normalized[field] = None
            else:
                normalized[field] = float(value) if isinstance(value, (int, float)) else value

        return normalized

    def fetch_core_statement_5y(
        self,
        stock_code: str,
        rpt_dates: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch 5-year core statement data for the MVP report skeleton.
        """
        dates = rpt_dates or [
            "20211231",
            "20221231",
            "20231231",
            "20241231",
            "20251231",
        ]

        rows: List[WindReportRow] = []
        for rpt_date in dates:
            snapshot = self.fetch_report_snapshot(
                stock_code=stock_code,
                rpt_date=rpt_date,
                fields=list(CORE_STATEMENT_FIELDS.keys()),
            )
            rows.append(
                WindReportRow(
                    rpt_date=rpt_date,
                    values={
                        key: snapshot.get(key) for key in CORE_STATEMENT_FIELDS.keys()
                    },
                )
            )

        return {
            "provider": "wind",
            "stock_code": stock_code,
            "field_labels": CORE_STATEMENT_FIELDS,
            "rows": [row.to_dict() for row in rows],
        }