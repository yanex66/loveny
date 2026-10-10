import requests
import random
import time

s = requests.Session()
s.headers.update({'User-Agent': 'Mozilla/5.0'})

# Get CSRF
r1 = s.get('https://loveny-3ct6.onrender.com/signup/')
csrf = s.cookies.get('csrftoken')

username = f"test_{random.randint(10000,99999)}"
data = {
    'csrfmiddlewaretoken': csrf,
    'username': username,
    'email': f'{username}@example.com',
    'password': 'Password123!',
    'password_confirm': 'Password123!',
    'relationship_mode': 'DATING'
}
r2 = s.post('https://loveny-3ct6.onrender.com/signup/', data=data, headers={'Referer': 'https://loveny-3ct6.onrender.com/signup/'})
print('Signup status:', r2.status_code, r2.url)

urls = [
    '/app/',
    '/matches/',
    '/messages/',
    '/profile/',
    '/settings/',
    '/hookup/',
    '/sex-call/',
]

for u in urls:
    time.sleep(0.5)
    r = s.get('https://loveny-3ct6.onrender.com' + u)
    print(f"URL: {u} - Status: {r.status_code} - Final URL: {r.url}")
    if r.status_code == 500:
        print("  => 500 ERROR FOUND!")

