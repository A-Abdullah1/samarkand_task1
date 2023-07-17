# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class PurchasePlanning(models.TransientModel):
    _name = "purchase.planning"
    _description = "Purchase planning based on vendors and related products"

    partner_id = fields.Many2one(comodel_name="res.partner", string="Vendor")
    date_begin = fields.Date(default=fields.Date.context_today, required=True,
                             help="Initial date to compute recommendations")
    date_end = fields.Date(default=fields.Date.context_today, required=True,
                           help="Final date to compute recommendations")
    show_all_partner_products = fields.Boolean(string="Show all supplier products", default=True,
                                               help="Show all products with supplier equal to this supplier")
    show_all_products = fields.Boolean(string="Show all purchasable products", default=False,
                                       help="Useful if a product hasn't been sold by a partner yet")
    product_category_ids = fields.Many2many(comodel_name="product.category", string="Product Categories",
                                            help="Filter by product internal category")
    warehouse_ids = fields.Many2many(comodel_name="stock.warehouse", string="Warehouse",
                                     help="Constrain search to a specific warehouse")
    warehouse_count = fields.Integer(default=lambda self: len(self.env["stock.warehouse"].search([])))

    @api.onchange('show_all_products')
    def onchange_show_all_products(self):
        if self.show_all_products:
            self.show_all_partner_products = False
            self.partner_id = False

    def _get_total_days(self):
        """Compute days between the initial and the end date"""
        day = (self.date_end + timedelta(days=1) - self.date_begin).days
        return day

    def _get_supplier_products(self):
        """Common method to be used for field domain filters"""
        supplierinfo_obj = self.env["product.supplierinfo"].with_context(prefetch_fields=False)
        partner = self.partner_id.commercial_partner_id
        supplierinfos = supplierinfo_obj.search([("name", "=", partner.id)])
        product_tmpls = supplierinfos.mapped("product_tmpl_id").filtered(
            lambda x: x.active and x.purchase_ok
        )
        products = supplierinfos.mapped("product_id").filtered(
            lambda x: x.active and x.purchase_ok
        )
        products += product_tmpls.mapped("product_variant_ids")
        return products

    def _get_products(self):
        """Override to filter products show_all_partner_products is set"""
        products = self._get_supplier_products()
        # Filter products by category if set.
        # It will apply to show_all_partner_products as well
        if self.product_category_ids:
            products = products.filtered(
                lambda x: x.categ_id in self.product_category_ids
            )
        return products

    def _get_move_line_domain(self, products, src, dst):
        """Allows to easily extend the domain by third modules"""
        combine = datetime.combine
        domain = [
            ("product_id", "in", products.ids),
            ("date", ">=", combine(self.date_begin, datetime.min.time())),
            ("date", "<=", combine(self.date_end, datetime.max.time())),
            ("location_id.usage", "=", src),
            ("location_dest_id.usage", "=", dst),
            ("state", "=", "done"),
        ]
        if self.warehouse_ids:
            domain += [
                (
                    "picking_id.picking_type_id.warehouse_id",
                    "in",
                    self.warehouse_ids.ids,
                )
            ]
        return domain

    def _get_all_products_domain(self):
        """Override to add more product filters if show_all_products is set"""
        domain = [
            ("purchase_ok", "=", True),
        ]
        if self.product_category_ids:
            domain += [("categ_id", "in", self.product_category_ids.ids)]
        return domain

    def _find_move_line(self, src="internal", dst="customer"):
        """"Returns a dictionary from the move lines in a range of dates
            from and to given location types"""
        products = self._get_products()
        domain = self._get_move_line_domain(products, src, dst)
        found_lines = self.env["stock.move.line"].read_group(
            domain, ["product_id", "qty_done"], ["product_id"]
        )
        # Manual ordering that circumvents ORM limitations
        found_lines = sorted(
            found_lines,
            key=lambda res: (res["product_id_count"], res["qty_done"],),
            reverse=True,
        )
        product_dict = {p.id: p for p in products}
        found_lines = [
            {
                "id": x["product_id"][0],
                "product_id": product_dict[x["product_id"][0]],
                "product_id_count": x["product_id_count"],
                "qty_done": x["qty_done"],
            }
            for x in found_lines
        ]
        found_lines = {line["id"]: line for line in found_lines}
        # Show every purchaseable product
        if self.show_all_products:
            products += self.env["product.product"].search(
                self._get_all_products_domain()
            )
        # Show all products with supplier infos belonging to a partner
        if self.show_all_partner_products or self.show_all_products:
            for product in products.filtered(lambda p: p.id not in found_lines.keys()):
                found_lines.update({product.id: {"product_id": product}})
        return found_lines

    @api.model
    def _prepare_wizard_line(self, vals):
        """Used to create the wizard line"""
        product_id = vals["product_id"]
        if self.warehouse_ids:
            units_available = sum(
                [
                    product_id.with_context(warehouse=wh).qty_available
                    for wh in self.warehouse_ids.ids
                ]
            )
            units_virtual_available = sum(
                [
                    product_id.with_context(warehouse=wh).virtual_available
                    for wh in self.warehouse_ids.ids
                ]
            )
        else:
            units_available = product_id.qty_available
            units_virtual_available = product_id.virtual_available
        price_unit = product_id._select_seller(
            partner_id=self.partner_id,
            date=fields.Date.today(),
            quantity=1,
            uom_id=product_id.uom_po_id,
        ).price
        currency_id = product_id._select_seller(
            partner_id=self.partner_id,
            date=fields.Date.today(),
            quantity=1,
            uom_id=product_id.uom_po_id,
        ).currency_id.id
        vendor_id = product_id._select_seller(
            partner_id=self.partner_id,
            date=fields.Date.today(),
            quantity=1,
            uom_id=product_id.uom_po_id,
        ).name.id
        return {
            "product_id": product_id.id,
            "partner_id": vendor_id,
            "times_delivered": vals.get("times_delivered", 0),
            "times_received": vals.get("times_received", 0),
            "units_received": vals.get("qty_received", 0),
            "units_available": units_available,
            "units_virtual_available": units_virtual_available,
            "units_avg_delivered": (vals.get("qty_delivered", 0) / self._get_total_days()),
            "units_delivered": vals.get("qty_delivered", 0),
            "price_unit": price_unit,
            "currency_id": currency_id,
        }

    def action_show_results(self):
        """Generate lines according to received and delivered items"""
        self.ensure_one()
        lines_obj = self.env['purchase.planning.line']
        lines_obj.search([]).unlink()
        # Get quantities received from suppliers
        found_dict = self._find_move_line(src="supplier", dst="internal")
        for product, line in found_dict.items():
            found_dict[product]["qty_received"] = line.get("qty_done", 0)
            found_dict[product]["times_received"] = line.get("product_id_count", 0)
        # Get quantities delivered to customers
        found_delivered_dict = self._find_move_line(src="internal", dst="customer")
        # Merge the two dicts
        for product, line in found_delivered_dict.items():
            if not found_dict.get(product):
                found_dict[product] = line
            found_dict[product]["qty_delivered"] = line.get("qty_done", 0)
            found_dict[product]["times_delivered"] = line.get("product_id_count", 0)
        for product, line in found_dict.items():
            new_line = self._prepare_wizard_line(line)
            lines_obj.create(new_line)

        return {
            'name': _('Purchase Planning for Products'),
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.planning.line',
            'view_mode': 'tree',
            'view_id':self.env.ref('purchase_order_product_recommendation.purchase_planning_line_view_tree').id,
            'search_view_id':self.env.ref('purchase_order_product_recommendation.purchase_planning_line_view_search').id,
        }


class PurchasePlanningLine(models.Model):
    _name = "purchase.planning.line"
    _description = "Products analysis based on the purchase planning selections"
    _order = "product_id"

    product_id = fields.Many2one(comodel_name="product.product", string="Product",)
    currency_id = fields.Many2one('res.currency', readonly=True,)
    partner_id = fields.Many2one('res.partner', readonly=True)
    price_unit = fields.Monetary(readonly=True,)
    times_delivered = fields.Integer(readonly=True,)
    times_received = fields.Integer(readonly=True,)
    units_received = fields.Float(readonly=True,)
    units_delivered = fields.Float(readonly=True,)
    units_avg_delivered = fields.Float(digits="Product Unit of Measure", readonly=True,)
    units_available = fields.Float(readonly=True,)
    units_virtual_available = fields.Float(readonly=True,)
