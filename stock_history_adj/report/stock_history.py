# -*- coding: utf-8 -*-
# Copyright 2020 Quartile Limited
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo import api, models
from odoo.fields import Datetime as fieldsDatetime

from odoo.addons.stock_account.report.stock_history import StockHistory


# Monkey-patching. The only point of adjustment is to add sudo() in the line
# https://github.com/OCA/OCB/blob/9259bc02ae3a8094ff5f612ef1f5800c8f7d0a5b/addons/stock_account/report/stock_history.py#L61
# to bypass the misterious product access error described in ticket 1076.
@api.model
def read_group(
    self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True
):
    # Step 1: retrieve the standard read_group output. In case of inventory valuation, this
    # will be mostly used as a 'skeleton' since the inventory value needs to be computed based
    # on the individual lines.
    res = super(StockHistory, self).read_group(
        domain, fields, groupby, offset=offset, limit=limit, orderby=orderby, lazy=lazy
    )
    if "inventory_value" in fields:
        groupby_list = groupby[:1] if lazy else groupby
        date = self._context.get("history_date", fieldsDatetime.now())

        # Step 2: retrieve the stock history lines. The result contains the 'expanded'
        # version of the read_group. We build the query manually for performance reason
        # (and avoid a costly 'WHERE id IN ...').
        fields_2 = set(
            ["id", "product_id", "price_unit_on_quant", "company_id", "quantity"]
            + groupby_list
        )
        query = self._where_calc(domain)
        self._apply_ir_rules(query, "read")
        tables, where_clause, where_clause_params = query.get_sql()
        select = "SELECT %s FROM %s WHERE %s "
        query = select % (",".join(fields_2), tables, where_clause)
        self._cr.execute(query, where_clause_params)

        # Step 3: match the lines retrieved at step 2 with the aggregated results of step 1.
        # In other words, we link each item of the read_group result with the corresponding
        # lines.
        stock_history_data = {}
        stock_histories_by_group = {}
        for line in self._cr.dictfetchall():
            stock_history_data[line["id"]] = line
            key = tuple(line.get(g) or False for g in groupby_list)
            stock_histories_by_group.setdefault(key, [])
            stock_histories_by_group[key] += [line["id"]]

        histories_dict = {}
        # sudo() added here (QRTL)
        not_real_cost_method_products = (
            self.env["product.product"]
            .sudo()
            .browse(record["product_id"] for record in stock_history_data.values())
            .filtered(lambda product: product.cost_method != "real")
        )
        if not_real_cost_method_products:
            self._cr.execute(
                """SELECT DISTINCT ON (product_id, company_id) product_id, company_id, cost
                FROM product_price_history
                WHERE product_id in %s AND datetime <= %s
                ORDER BY product_id, company_id, datetime DESC, id DESC""",
                (tuple(not_real_cost_method_products.ids), date),
            )
            for history in self._cr.dictfetchall():
                histories_dict[
                    (history["product_id"], history["company_id"])
                ] = history["cost"]

        for line in res:
            inv_value = 0.0
            # Build the same keys than above, but need to take into account Many2one are tuples
            key = tuple(line[g] if g in line else False for g in groupby_list)
            key = tuple(k[0] if isinstance(k, tuple) else k for k in key)
            for stock_history in self.env["stock.history"].browse(
                stock_histories_by_group[key]
            ):
                history_data = stock_history_data[stock_history.id]
                product_id = history_data["product_id"]
                if self.env["product.product"].browse(product_id).cost_method == "real":
                    price = history_data["price_unit_on_quant"]
                else:
                    price = histories_dict.get(
                        (product_id, history_data["company_id"]), 0.0
                    )
                inv_value += price * history_data["quantity"]
            line["inventory_value"] = inv_value

    return res


class StockHistoryHookReadGroup(models.AbstractModel):
    _name = "stock.history.hook.read.group"
    _description = "Provide hook point for read_group method"

    def _register_hook(self):
        StockHistory.read_group = read_group
        return super(StockHistoryHookReadGroup, self)._register_hook()
