import random
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from dating.models import Profile, Tag, GENDER_CHOICES

class Command(BaseCommand):
    help = 'Creates 1000 test users and associated profiles.'

    def handle(self, *args, **options):
        self.stdout.write("--- Starting LOVENY Test Data Generation ---")
        
        NUM_USERS = 1000
        
        # --- 1. Data Pools ---
        TAG_NAMES = [
            'BookWorm', 'MovieBuff', 'Traveler', 'HomeBody', 'FitnessFan', 'Foodie', 
            'NightOwl', 'EarlyBird', 'DogLover', 'CatParent', 'WineEnthusiast', 
            'CoffeeAddict', 'DIYProjects', 'ConcertGoer', 'Volunteering', 'Gamer', 
            'Musician', 'Artist', 'Photographer', 'TechGeek', 'Afrobeats', 'Nollywood'
        ]

        LOCATIONS = [
            'Lagos, NG', 'Abuja, NG', 'Port Harcourt, NG', 'Ibadan, NG', 
            'Lekki, Lagos', 'Ikeja, Lagos', 'Yaba, Lagos', 'Enugu, NG', 
            'Kano, NG', 'Benin City, NG', 'Victoria Island, Lagos'
        ]

        JOBS = [
            'Software Engineer', 'Entrepreneur', 'Nurse', 'Doctor', 'Teacher',
            'Digital Marketer', 'Content Creator', 'Chef', 'Lawyer', 'Banker',
            'Fashion Designer', 'Student', 'Architect', 'Trader', 'Makeup Artist'
        ]
        
        # --- 2. Create Tags ---
        all_tags = []
        for name in TAG_NAMES:
            tag, created = Tag.objects.get_or_create(name=name)
            all_tags.append(tag)
        self.stdout.write(self.style.SUCCESS(f'Ensured {len(all_tags)} tags exist.'))

        # --- 3. Create Users ---
        self.stdout.write(f"Generating {NUM_USERS} users...")
        
        new_users_count = 0
        
        for i in range(1, NUM_USERS + 1):
            username = f'user_{i}'
            email = f'{username}@loveny.com'
            
            # Skip if user exists
            if User.objects.filter(username=username).exists():
                continue
                
            # Create User
            user = User.objects.create_user(username=username, email=email, password='password123')
            
            # Random Logic
            gender = random.choice([choice[0] for choice in GENDER_CHOICES])
            
            # Smart matching preferences
            if gender == 'M':
                preferred_gender = 'F'
            elif gender == 'F':
                preferred_gender = 'M'
            else:
                preferred_gender = random.choice(['M', 'F'])
            
            # Unique WhatsApp: 23480 + unique sequence
            whatsapp_number = f'23480{i:06d}' 

            # Create Profile
            profile = Profile.objects.create(
                user=user,
                age=random.randint(18, 45),
                gender=gender,
                preferred_gender=preferred_gender,
                bio=f"Just a {random.choice(['chill', 'fun', 'serious', 'happy'])} person looking for a vibe in {random.choice(LOCATIONS).split(',')[0]}.",
                location=random.choice(LOCATIONS),
                job_title=random.choice(JOBS),
                whatsapp_number=whatsapp_number,
                first_date_idea=random.choice([
                    "Dinner at a nice spot on the mainland.",
                    "Movies and popcorn.",
                    "Beach day at Elegushi.",
                    "Coffee and good conversation.",
                    "Amala date, nothing fancy."
                ]),
                last_active=timezone.now() - timedelta(minutes=random.randint(1, 10000))
            )

            # Assign Random Tags
            num_tags = random.randint(2, 5)
            if all_tags:
                profile.tags.set(random.sample(all_tags, num_tags))
            
            new_users_count += 1
            
            # Progress indicator every 100 users
            if i % 100 == 0:
                self.stdout.write(f"... created {i} users")

        self.stdout.write(self.style.SUCCESS(f'DONE! Created {new_users_count} new users.'))