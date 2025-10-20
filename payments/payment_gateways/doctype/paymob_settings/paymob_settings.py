# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe import _
from payments.payment_gateways.paymob.accept_api import AcceptAPI
from payments.payment_gateways.paymob.connection import AcceptConnection
from payments.payment_gateways.paymob.paymob_urls import PaymobUrls
from payments.payment_gateways.paymob.hmac_validator import HMACValidator

from frappe.integrations.utils import (
	create_request_log,

)
from payments.payment_gateways.paymob.response_codes import SUCCESS
class PaymobSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password
		hmac: DF.Password
		iframe: DF.Data
		payment_integration: DF.Int
		public_key: DF.Password
		secret_key: DF.Password
		token: DF.Password | None
	# end: auto-generated types
	
	@frappe.whitelist()
	def get_access_token(self):
		accept = AcceptAPI()
		token = accept.retrieve_auth_token()
		return token
	

	def get_payment_url(self, **kwargs):
		try:
			connection = AcceptConnection()
			paymob_urls = PaymobUrls()

			if not kwargs.get("order_id") or not kwargs.get("amount"):
				frappe.throw(_("Missing order ID or amount"))

			# Build dummy billing data (Paymob requires this)
			billing_data = {
				"apartment": "NA",
				"email": kwargs.get("payer_email"),
				"floor": "NA",
				"first_name": kwargs.get("payer_name").split()[0],
				"street": "NA",
				"building": "NA",
				"phone_number": "+201111111111",  
				"shipping_method": "NA",
				"postal_code": "NA",
				"city": "Cairo",
				"country": "EG",
				"last_name": kwargs.get("payer_name").split()[-1],
				"state": "NA"
			}

			payment_key_payload = {
				"auth_token": self.get_password("token"),
				"amount_cents": str(int(float(kwargs.get("amount")) * 100)),
				"expiration": 3600,
				"order_id": kwargs.get("order_id"),
				"currency": kwargs.get("currency", "EGP"),
				"billing_data":billing_data,
				"integration_id": self.payment_integration,
			}

			# url = "https://accept.paymob.com/api/acceptance/payment_keys"
			url = paymob_urls.get_url("payment_key")
			code, feedback = connection.post(url=url, json=payment_key_payload)

			if code != SUCCESS or "token" not in feedback.data:
				frappe.throw(_("Failed to retrieve payment token from Paymob"))

			payment_token = feedback.data["token"]

			iframe_url = f"https://accept.paymob.com/api/acceptance/iframes/{self.iframe}?payment_token={payment_token}"
			return iframe_url

		except Exception:
			frappe.log_error(frappe.get_traceback())
			frappe.throw(_("Could not generate Paymob payment URL"))



	def create_order(self, **kwargs):
		
		# Create integration log
		integration_request = create_request_log(kwargs, service_name="Paymob")
		connection=AcceptConnection()
		paymob_urls = PaymobUrls()

		# Get your API token
		token = self.get_password("token")

		# Required fields by Paymob
		amount_cents = int(kwargs.get("amount")) * 100  # Paymob uses cents
		currency = kwargs.get("currency", "EGP")
		delivery_needed = kwargs.get("delivery_needed", False)
		items = kwargs.get("items", [])  # Required field even if empty

		# Construct payload
		payload = {
			"auth_token": token,
			"delivery_needed": str(delivery_needed).lower(),
			"amount_cents": str(amount_cents),
			"currency": currency,
			"items": items,
		}

		try:
			# url = "https://accept.paymob.com/api/ecommerce/orders"
			url=paymob_urls.get_url("order")
			code, feedback = connection.post(url=url, json=payload)
			if code != SUCCESS:
				frappe.throw(_("Failed to create order in Paymob"))
				
			order=feedback.data
			order["integration_request"] = integration_request.name
			return order
		except Exception:
			frappe.log_error(frappe.get_traceback())
			frappe.throw(_("Could not create Paymob order"))


@frappe.whitelist(allow_guest=True)
def callback():
	try:
		# Extract the HMAC from request parameters (query string or form data)
		incoming_hmac = frappe.request.args.get("hmac") or frappe.request.form.get("hmac")
		
		if not incoming_hmac:
			return "Missing HMAC"

		# Get the callback JSON data from request body
		incoming_data_json = frappe.request.get_json()
		print("JSON data:", incoming_data_json)

		if not incoming_data_json:
			return "Invalid or missing data"

		# Validate the HMAC
		validator = HMACValidator(
			incoming_hmac=incoming_hmac,
			callback_dict=incoming_data_json
		)

		if not validator.is_valid:
			return "Invalid HMAC"

		# HMAC is valid, extract transaction data
		obj_data = incoming_data_json.get("obj", {})
		success = obj_data.get("success")

		billing_data = obj_data.get("payment_key_claims", {}).get("billing_data", {})
		payer_email=billing_data.get("email",)
		
		# Check if transaction succeeded
		if success is True or str(success).lower() == "true":
			# Get payment and order IDs
			paymob_payment_id = obj_data.get("id")
			paymob_order_id = obj_data.get("order", {}).get("id")

			if not paymob_order_id:
				return "Missing order ID"

			# Find the Event Payment document based on order_id stored in Integration Request
			integration_requests = frappe.get_all(
				"Integration Request",
				filters={
					"data": ["like", f'%\"payer_email\": \"{payer_email}\"%'],
					"integration_request_service": "Paymob"
				},
				fields=["name", "data"],
				order_by="creation desc",
				limit=1
			)

			if not integration_requests:
				return f"No matching order found for Paymob order ID: {paymob_order_id}"

			# Parse the integration request data to get payment reference
			import json
			integration_data = json.loads(integration_requests[0].data)
			event_payment_id = integration_data.get("payment")

			if not event_payment_id:
				return "No payment reference found"

			# Update the Event Payment document
			payment_doc = frappe.get_doc("Event Payment", event_payment_id)
			payment_doc.payment_received = 1
			payment_doc.payment_id = str(paymob_payment_id)
			payment_doc.order_id = str(paymob_order_id)
			payment_doc.flags.ignore_permissions = True
			payment_doc.save()
			frappe.db.commit()

			return "Payment verified successfully"
		else:
			# Payment failed or was cancelled
			return "Payment failed or was cancelled"

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Paymob Callback Error")
		return "Server error"




@frappe.whitelist()
def update_paymob_settings(**kwargs):
	args = frappe._dict(kwargs)
	fields = frappe._dict(
		{
			"api_key": args.get("api_key"),
			"secret_key": args.get("secret_key"),
			"public_key": args.get("public_key"), 
			"hmac": args.get("hmac"), 
			"iframe": args.get("iframe"), 
			"payment_integration": args.get("payment_integration"), 
		}
	)
	try:
		paymob_settings = frappe.get_doc("Paymob Settings").update(fields)
		paymob_settings.save()
		return "Paymob Credentials Successfully"
	except Exception as e:
		return "Failed to Update Paymob Credentials"