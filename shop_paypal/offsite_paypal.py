#-*- coding: utf-8 -*-
from decimal import Decimal

from django.conf import settings
from django.conf.urls.defaults import patterns, url, include
from django.contrib.sites.models import get_current_site
from django.core.urlresolvers import reverse
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponseRedirect

from paypal.standard.forms import PayPalPaymentsForm
from paypal.standard.ipn.signals import payment_was_successful as success_signal
from shop import order_signals
from shop.models import Order

from utils import generate_key
IPN_RETURN_KEY = generate_key(96, 1024)

import logging
logger = logging.getLogger('paypal')

class OffsitePaypalBackend(object):
    '''
    Glue code to let django-SHOP talk to django-paypal's.

    The django-paypal package already defines an IPN view, that logs everything
    to the database (desirable), and fires up a signal.
    It is therefore more convenient to listen to the signal instead of rewriting
    the ipn view (and necessary tests)
    '''

    backend_name = "Paypal"
    url_namespace = "paypal"

    #===========================================================================
    # Defined by the backends API
    #===========================================================================

    def __init__(self, shop):
        self.shop = shop
        # Hook the payment was successful listener on the appropriate signal sent
        # by django-paypal (success_signal)
        success_signal.connect(self.payment_was_successful, weak=False)
        assert settings.PAYPAL_RECEIVER_EMAIL, "You need to define a PAYPAL_RECEIVER_EMAIL in settings with the money recipient's email addresss"
        assert settings.PAYPAL_CURRENCY_CODE, "You need to define a PAYPAL_CURRENCY_CODE in settings with the currency code"

    def get_urls(self):
        urlpatterns = patterns('',
            url(r'^$', self.view_that_asks_for_money, name='paypal'),
            url(r'^success/$', self.paypal_successful_return_view, name='paypal_success'),
            url(r'^ipn/{0}/$'.format(IPN_RETURN_KEY), include('paypal.standard.ipn.urls')),
        )
        return urlpatterns

    def get_form(self, request):
        '''
        Configures a paypal form and returns. Allows this code to be reused
        in other views.
        '''
        order = self.shop.get_order(request)
        url_scheme = 'https' if request.is_secure() else 'http'
        # get_current_site requires Django 1.3 - backward compatibility?
        url_domain = get_current_site(request).domain
        paypal_dict = {
        "business": settings.PAYPAL_RECEIVER_EMAIL,
        "currency_code": settings.PAYPAL_CURRENCY_CODE,
        "amount": self.shop.get_order_total(order),
        "item_name": settings.PAYPAL_ITEM_NAME,
        "invoice": order.get_number(),
        "notify_url": '%s://%s%s' % (url_scheme,
            url_domain, reverse('paypal-ipn')),  # defined by django-paypal
        "return_url": '%s://%s%s' % (url_scheme,
            url_domain, reverse('paypal_success')),  # That's this classe's view
        "cancel_return": '%s://%s%s' % (url_scheme,
            url_domain, self.shop.get_cancel_url()),  # A generic one
        }
        if hasattr(settings, 'PAYPAL_LC'):
            paypal_dict['lc'] = settings.PAYPAL_LC

        # Create the instance.
        form = PayPalPaymentsForm(initial=paypal_dict)
        return form

    #===========================================================================
    # Views
    #===========================================================================

    def view_that_asks_for_money(self, request):
        '''
        We need this to be a method and not a function, since we need to have
        a reference to the shop interface
        '''
        form = self.get_form(request)
        order = self.shop.get_order(request)
        #order_signals.confirmed.send(sender=self, order=order)
        context = {"form": form, 'order' : order}
        rc = RequestContext(request, context)
        return render_to_response("shop_paypal/payment.html", rc)

    @csrf_exempt
    def paypal_successful_return_view(self, request):
        return HttpResponseRedirect(self.shop.get_finished_url())

    #===========================================================================
    # Signal listeners
    #===========================================================================

    def payment_was_successful(self, sender, **kwargs):
        '''
        This listens to the signal emitted by django-paypal's IPN view and in turn
        asks the shop system to record a successful payment.
        '''
        ipn_obj = sender
        order_id = ipn_obj.invoice  # That's the "invoice ID we passed to paypal
        amount = Decimal(ipn_obj.mc_gross)
        transaction_id = ipn_obj.txn_id

        logger.info("Successful payment : transaction_id: {transaction_id}, Sender: {sender}, OrderID {order_id}, Total: {total}".format(
          transaction_id=transaction_id,
          sender = sender,
          order_id = order_id,
          total = amount))

        # The actual request to the shop system
        order = Order.objects.get_for_number(order_id)
        order_signals.completed.send(sender=self, order=order)
        self.shop.confirm_payment(order, amount, transaction_id, self.backend_name)
