from django.shortcuts import redirect
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib import messages
from django.conf import settings

from amc.tokens import account_activation_token_generator

def login_with_token(request):
  uidb64 = request.GET.get('uidb64')
  token = request.GET.get('token')

  if not uidb64 or not token:
    messages.error(request, 'Invalid login link. The link is incomplete.')
    return redirect(settings.SITE_DOMAIN)

  try:
    uid = force_str(urlsafe_base64_decode(uidb64))
    user = User.objects.get(pk=uid)
  except (TypeError, ValueError, OverflowError, User.DoesNotExist):
    user = None

  if user is not None and account_activation_token_generator.check_token(user, token):
    login(request, user)
    return redirect(settings.SITE_DOMAIN)
  else:
    return redirect(settings.SITE_DOMAIN)

