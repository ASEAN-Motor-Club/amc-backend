from django.shortcuts import redirect
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib import messages
from django.conf import settings
from urllib.parse import urljoin

from amc.tokens import account_activation_token_generator

def login_with_token(request):
  uidb64 = request.GET.get('uidb64')
  token = request.GET.get('token')
  next_url = request.GET.get('next', '/')

  # Security: Only allow relative paths to prevent open redirect attacks
  if next_url and (next_url.startswith('//') or '://' in next_url):
    next_url = '/'

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
    # Redirect to the requested path, or fall back to site domain
    redirect_url = urljoin(settings.SITE_DOMAIN, next_url)
    return redirect(redirect_url)
  else:
    messages.error(request, 'Invalid or expired login link.')
    return redirect(settings.SITE_DOMAIN)
