import os
import django
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'loveny_site.settings')
django.setup()

from django.test import Client
from django.contrib.auth.models import User
from dating.models import Profile

c = Client()
u = User.objects.filter(username='test_err').first()
if not u:
    u = User.objects.create_user(username='test_err', email='err@err.com', password='pass123')
else:
    u.set_password('pass123')
    u.save()

Profile.objects.filter(user=u).delete()
Profile.objects.create(user=u, relationship_mode='DATING', gender='M', age=25)

c.login(username='test_err', password='pass123')
try:
    res = c.get('/app/', HTTP_HOST='localhost')
    print("STATUS CODE:", res.status_code)
except Exception as e:
    import traceback
    with open('traceback.txt', 'w') as f:
        traceback.print_exc(file=f)
    print("Caught exception! Saved to traceback.txt")
