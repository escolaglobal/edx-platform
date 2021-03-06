""" E-Commerce API client """

import json
import logging

from django.conf import settings
import jwt
import requests
from requests import Timeout
from rest_framework.status import HTTP_200_OK

from commerce.exceptions import InvalidResponseError, TimeoutError, InvalidConfigurationError


log = logging.getLogger(__name__)


class EcommerceAPI(object):
    """ E-Commerce API client. """

    def __init__(self, url=None, key=None, timeout=None):
        self.url = url or settings.ECOMMERCE_API_URL
        self.key = key or settings.ECOMMERCE_API_SIGNING_KEY
        self.timeout = timeout or getattr(settings, 'ECOMMERCE_API_TIMEOUT', 5)

        if not (self.url and self.key):
            raise InvalidConfigurationError('Values for both url and key must be set.')

        # Remove slashes, so that we can properly format URLs regardless of
        # whether the input includes a trailing slash.
        self.url = self.url.strip('/')

    def _get_jwt(self, user):
        """
        Returns a JWT object with the specified user's info.

        Raises AttributeError if settings.ECOMMERCE_API_SIGNING_KEY is not set.
        """
        data = {
            'username': user.username,
            'email': user.email
        }
        return jwt.encode(data, self.key)

    def get_order(self, user, order_number):
        """
        Retrieve a paid order.

        Arguments
            user             --  User associated with the requested order.
            order_number     --  The unique identifier for the order.

        Returns a tuple with the order number, order status, API response data.
        """
        def get():
            """Internal service call to retrieve an order. """
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'JWT {}'.format(self._get_jwt(user))
            }
            url = '{base_url}/orders/{order_number}/'.format(base_url=self.url, order_number=order_number)
            return requests.get(url, headers=headers, timeout=self.timeout)

        data = self._call_ecommerce_service(get)
        return data['number'], data['status'], data

    def get_processors(self, user):
        """
        Retrieve the list of available payment processors.

        Returns a list of strings.
        """
        def get():
            """Internal service call to retrieve the processor list. """
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'JWT {}'.format(self._get_jwt(user))
            }
            url = '{base_url}/payment/processors/'.format(base_url=self.url)
            return requests.get(url, headers=headers, timeout=self.timeout)

        return self._call_ecommerce_service(get)

    def create_basket(self, user, sku, payment_processor=None):
        """Create a new basket and immediately trigger checkout.

        Note that while the API supports deferring checkout to a separate step,
        as well as adding multiple products to the basket, this client does not
        currently need that capability, so that case is not supported.

        Args:
            user: the django.auth.User for which the basket should be created.
            sku: a string containing the SKU of the course seat being ordered.
            payment_processor: (optional) the name of the payment processor to
                use for checkout.

        Returns:
            A dictionary containing {id, order, payment_data}.

        Raises:
            TimeoutError: the request to the API server timed out.
            InvalidResponseError: the API server response was not understood.
        """
        def create():
            """Internal service call to create a basket. """
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'JWT {}'.format(self._get_jwt(user))
            }
            url = '{}/baskets/'.format(self.url)
            data = {'products': [{'sku': sku}], 'checkout': True, 'payment_processor_name': payment_processor}
            return requests.post(url, data=json.dumps(data), headers=headers, timeout=self.timeout)

        return self._call_ecommerce_service(create)

    @staticmethod
    def _call_ecommerce_service(call):
        """
        Makes a call to the E-Commerce Service. There are a number of common errors that could occur across any
        request to the E-Commerce Service that this helper method can wrap each call with. This method helps ensure
        calls to the E-Commerce Service will conform to the same output.

        Arguments
            call    --  A callable function that makes a request to the E-Commerce Service.

        Returns a dict of JSON-decoded API response data.
        """
        try:
            response = call()
            data = response.json()
        except Timeout:
            msg = 'E-Commerce API request timed out.'
            log.error(msg)
            raise TimeoutError(msg)
        except ValueError:
            msg = 'E-Commerce API response is not valid JSON.'
            log.exception(msg)
            raise InvalidResponseError(msg)

        status_code = response.status_code

        if status_code == HTTP_200_OK:
            return data
        else:
            msg = u'Response from E-Commerce API was invalid: (%(status)d) - %(msg)s'
            msg_kwargs = {
                'status': status_code,
                'msg': data.get('user_message'),
            }
            log.error(msg, msg_kwargs)
            raise InvalidResponseError(msg % msg_kwargs)
