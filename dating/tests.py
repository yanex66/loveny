import json
import tempfile
from io import StringIO
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django import forms
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command, CommandError
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import ProfileAdminForm
from .forms import ProfileCreationForm, ProfileForm, SettingsForm
from .models import (
    CallGift,
    CallSession,
    ChatMessage,
    CoinPackage,
    CoinTransaction,
    CoinWallet,
    Conversation,
    GiftItem,
    Match,
    PaymentTransaction,
    Profile,
    ProfilePhoto,
    SiteConfiguration,
    SubscriptionPlan,
    Swipe,
)
from .views import _provider_payment, get_profile_batch


class DatingPlatformTests(TestCase):
    def make_profile(
        self,
        username,
        *,
        age=28,
        gender='M',
        preferred_gender='F',
        mode='DATING',
        latitude=None,
        longitude=None,
        is_vip=False,
    ):
        user = User.objects.create_user(
            username=username,
            email=f'{username}@example.com',
            password='test-password-123',
        )
        profile = Profile.objects.create(
            user=user,
            age=age,
            gender=gender,
            preferred_gender=preferred_gender,
            relationship_mode=mode,
            latitude=latitude,
            longitude=longitude,
            is_vip=is_vip,
            whatsapp_number=f'+234{user.pk:010d}',
            last_active=timezone.now(),
        )
        return user, profile

    def setUp(self):
        self.alice, self.alice_profile = self.make_profile(
            'alice', age=26, gender='F', preferred_gender='M'
        )
        self.bob, self.bob_profile = self.make_profile('bob')

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def test_admin_profile_premium_tiers_are_plan_selectors(self):
        profile = self.alice_profile
        SubscriptionPlan.objects.create(
            category='DATING',
            tier_name='Gold',
            duration_days=365,
            price=Decimal('1000.00'),
        )

        form = ProfileAdminForm(instance=profile)

        self.assertIsInstance(form.fields['dating_premium_tier'], forms.ChoiceField)
        self.assertIn(
            ('Gold', 'Gold'),
            form.fields['dating_premium_tier'].choices,
        )
        self.assertNotIn('premium_tier', form.fields)
        self.assertNotIn('premium_expiry', form.fields)
        self.assertNotIn('whatsapp_number', form.fields)

    def test_discovery_excludes_self_acted_profiles_and_other_modes(self):
        self.client.force_login(self.alice)
        self.assertEqual([p.user_id for p in get_profile_batch(self.alice)], [self.bob.pk])

        Swipe.objects.create(swiper=self.alice, swiped=self.bob, type='PASS')
        self.assertEqual(get_profile_batch(self.alice), [])

        Swipe.objects.filter(swiper=self.alice, swiped=self.bob).delete()
        _, hookup_profile = self.make_profile(
            'hookup', mode='HOOKUP', latitude=0, longitude=0
        )
        self.assertNotIn(hookup_profile, get_profile_batch(self.alice))

    def test_test_profiles_are_hidden_from_members_and_staff_preview_is_explicit(self):
        self.bob_profile.is_test_profile = True
        self.bob_profile.is_verified = False
        self.bob_profile.save(update_fields=['is_test_profile', 'is_verified'])
        self.bob.is_active = False
        self.bob.save(update_fields=['is_active'])

        self.client.force_login(self.alice)
        response = self.client.get(reverse('get_profiles_json'))
        self.assertEqual(response.json()['profiles'], [])

        staff, _ = self.make_profile(
            'staff-preview',
            age=30,
            gender='F',
            preferred_gender='M',
        )
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        self.client.force_login(staff)
        session = self.client.session
        session['staff_test_profile_preview'] = True
        session.save()

        response = self.client.get(reverse('get_profiles_json'))
        self.assertEqual(len(response.json()['profiles']), 1)
        self.assertEqual(response.json()['profiles'][0]['id'], self.bob.pk)
        self.assertTrue(response.json()['profiles'][0]['is_test_profile'])
        self.assertFalse(response.json()['profiles'][0]['is_verified'])

    def test_seed_200_users_creates_disabled_staff_only_profiles_and_avatars(self):
        with self.assertRaises(CommandError):
            call_command('seed_200_users', stdout=StringIO())

        User.objects.create_superuser(
            username='seed-admin',
            email='seed-admin@example.com',
            password='safe-admin-password',
        )
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                output = StringIO()
                call_command('seed_200_users', staff_only=True, stdout=output)

                profiles = Profile.objects.filter(is_test_profile=True)
                self.assertEqual(profiles.count(), 200)
                self.assertEqual(
                    profiles.filter(relationship_mode='DATING').count(),
                    80,
                )
                self.assertEqual(
                    profiles.filter(relationship_mode='HOOKUP').count(),
                    60,
                )
                self.assertEqual(
                    profiles.filter(relationship_mode='SEX_CALL').count(),
                    60,
                )
                first_user = User.objects.get(username='test_user_1001')
                self.assertFalse(first_user.is_active)
                self.assertFalse(first_user.has_usable_password())
                first_profile = first_user.profile
                self.assertFalse(first_profile.is_verified)
                self.assertTrue(first_profile.show_in_discovery)
                self.assertTrue(first_profile.photos.filter(is_main=True).exists())
                photos = ProfilePhoto.objects.filter(profile__in=profiles)
                self.assertEqual(photos.count(), 200)
                image_names = set(photos.values_list('image', flat=True))
                self.assertEqual(len(image_names), 2)
                self.assertTrue(all(default_storage.exists(name) for name in image_names))
                self.assertIn('Total isolated test profiles: 200', output.getvalue())

    def test_hookup_discovery_applies_distance_filter(self):
        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.latitude = 0
        self.alice_profile.longitude = 0
        self.alice_profile.max_distance_km = 30
        self.alice_profile.save()
        self.bob_profile.relationship_mode = 'HOOKUP'
        self.bob_profile.latitude = 0.1
        self.bob_profile.longitude = 0
        self.bob_profile.save()
        _, far_profile = self.make_profile(
            'far', mode='HOOKUP', latitude=2, longitude=0
        )

        self.assertEqual([p.user_id for p in get_profile_batch(self.alice)], [self.bob.pk])
        self.assertNotIn(far_profile, get_profile_batch(self.alice))

    def test_sex_call_mode_is_available_in_creation_settings_and_discovery(self):
        mode_choices = Profile._meta.get_field('relationship_mode').choices
        self.assertEqual(
            list(mode_choices),
            [('DATING', 'Dating'), ('HOOKUP', 'Hookup'), ('SEX_CALL', 'Sex Call')],
        )
        profile_fields = {field.name for field in Profile._meta.get_fields()}
        self.assertFalse(
            {'sugar_role', 'sugar_arrangement', 'sugar_allowance', 'sugar_lifestyle'}
            & profile_fields
        )
        self.assertIn(('SEX_CALL', 'Sex Call'), mode_choices)
        self.assertNotIn('relationship_mode', ProfileForm().fields)
        self.assertIn('relationship_mode', ProfileCreationForm().fields)
        self.assertEqual(
            ProfileCreationForm(initial={'relationship_mode': 'SEX_CALL'})
            ['relationship_mode'].value(),
            'SEX_CALL',
        )
        self.assertIn(
            ('SEX_CALL', 'Sex Call'),
            SettingsForm(instance=self.alice_profile).fields['relationship_mode'].choices,
        )

        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save()
        self.bob_profile.relationship_mode = 'SEX_CALL'
        self.bob_profile.save()
        self.assertEqual(
            [profile.user_id for profile in get_profile_batch(self.alice)],
            [self.bob.pk],
        )

        self.alice_profile.hookup_premium_expiry = timezone.now() + timedelta(days=1)
        self.alice_profile.hookup_premium_tier = 'Gold'
        self.alice_profile.save()
        self.assertTrue(self.alice_profile.is_hookup_premium)
        self.assertTrue(self.alice_profile.is_premium('HOOKUP'))
        self.assertFalse(self.alice_profile.is_premium())
        self.assertFalse(self.alice_profile.is_dating_premium)
        self.assertFalse(self.alice_profile.is_sex_call_premium)

    def test_profile_form_limits_interests_to_five(self):
        tags = list(ProfileForm().fields['tags'].queryset[:6])
        self.assertEqual(len(tags), 6)
        form = ProfileForm(data={
            'age': 26,
            'gender': 'F',
            'preferred_gender': 'M',
            'min_age_pref': 18,
            'max_age_pref': 50,
            'whatsapp_number': '+234555555555',
            'tags': [tag.pk for tag in tags],
        })
        self.assertFalse(form.is_valid())
        self.assertIn('Choose up to five interests.', form.errors['tags'])

    def test_test_data_command_creates_idempotent_profiles_with_avatars(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                call_command('create_test_data', count=3, stdout=StringIO())
                call_command('create_test_data', count=3, stdout=StringIO())

                test_profiles = Profile.objects.filter(
                    user__username__startswith='test_profile_',
                )
                self.assertEqual(test_profiles.count(), 3)
                self.assertEqual(ProfilePhoto.objects.filter(profile__in=test_profiles).count(), 3)
                self.assertTrue(
                    all(profile.photos.first().image.name.endswith('.svg') for profile in test_profiles)
                )
                self.assertTrue(all(profile.tags.count() == 5 for profile in test_profiles))

    def test_seeded_test_profiles_cover_same_gender_discovery_preferences(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                call_command('create_test_data', count=27, stdout=StringIO())
                same_gender_user, _ = self.make_profile(
                    'same-gender-seeker',
                    gender='M',
                    preferred_gender='M',
                )
                candidates = get_profile_batch(same_gender_user, limit=1000)
                self.assertTrue(candidates)
                self.assertTrue(all(profile.relationship_mode == 'DATING' for profile in candidates))
                self.assertTrue(all(profile.gender == 'M' for profile in candidates))
                self.assertTrue(all(profile.preferred_gender == 'M' for profile in candidates))
                self.client.force_login(same_gender_user)
                response = self.client.get(reverse('get_profiles_json'))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['profiles'])
                self.assertTrue(all(
                    profile['relationship_mode'] == 'DATING'
                    for profile in response.json()['profiles']
                ))

    def test_repair_test_profile_photos_replaces_missing_files(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                call_command('create_test_data', count=1, stdout=StringIO())
                profile = Profile.objects.get(user__username='test_profile_0001')
                default_storage.delete(profile.photos.first().image.name)

                call_command('repair_test_profile_photos', stdout=StringIO())

                photo = profile.photos.get()
                self.assertTrue(photo.is_main)
                self.assertTrue(default_storage.exists(photo.image.name))
                self.assertTrue(photo.image.name.endswith('.svg'))

    def test_edit_profile_has_tabs_and_updates_account_name(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse('edit_profile'))
        self.assertContains(response, 'Photos &amp; Media')
        self.assertContains(response, 'Discovery &amp; Settings')
        self.assertContains(response, 'Use current GPS location')

        response = self.client.post(reverse('edit_profile'), {
            'name': 'Alice Newname',
            'age': 26,
            'gender': 'F',
            'preferred_gender': 'M',
            'min_age_pref': 18,
            'max_age_pref': 50,
            'whatsapp_number': self.alice_profile.whatsapp_number,
            'location': 'Lagos',
            'job_title': 'Designer',
            'bio': 'A little about me.',
            'first_date_idea': 'Coffee and a walk.',
            'max_distance_km': 100,
        })
        self.assertRedirects(response, f"{reverse('edit_profile')}?saved=1")
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.first_name, 'Alice Newname')

    def test_signup_and_login_require_and_store_connection_mode(self):
        self.assertContains(
            self.client.get(reverse('account_login')),
            'Connection mode',
        )
        response = self.client.post(reverse('signup'), {
            'username': 'newmember',
            'email': 'newmember@example.com',
            'first_name': 'New',
            'password1': 'secure-password-123',
            'password2': 'secure-password-123',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='newmember').exists())

        response = self.client.post(reverse('signup'), {
            'username': 'newmember',
            'email': 'newmember@example.com',
            'first_name': 'New',
            'password1': 'secure-password-123',
            'password2': 'secure-password-123',
            'relationship_mode': 'SEX_CALL',
        })
        self.assertRedirects(response, reverse('create_profile'))
        self.assertEqual(self.client.session['active_connection_mode'], 'SEX_CALL')
        self.assertContains(self.client.get(reverse('create_profile')), 'Sex Call')

        self.client.logout()
        response = self.client.post(reverse('login'), {
            'username': 'alice',
            'password': 'test-password-123',
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

        response = self.client.post(reverse('login'), {
            'username': 'alice',
            'password': 'test-password-123',
            'relationship_mode': 'HOOKUP',
        })
        self.assertRedirects(response, reverse('swipe_card'))
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.relationship_mode, 'HOOKUP')
        self.assertEqual(self.client.session['active_connection_mode'], 'HOOKUP')

    def test_auth_pages_show_concise_password_guidance(self):
        signup = self.client.get(reverse('signup'))
        self.assertContains(
            signup,
            'Use 8+ characters and avoid common or personal-info-based passwords.',
        )
        self.assertNotContains(signup, 'Your password can’t be too similar')
        self.assertNotContains(signup, 'Your password can’t be a commonly used password')

        login_page = self.client.get(reverse('login'))
        self.assertNotContains(login_page, 'Your password must contain at least 8 characters')
        self.assertNotContains(login_page, 'commonly used password')

        self.client.force_login(self.alice)
        password_change = self.client.get(reverse('password_change'))
        self.assertContains(
            password_change,
            'Use 8+ characters and avoid common or personal-info-based passwords.',
        )
        self.assertNotContains(password_change, 'Your password can’t be too similar')
        self.assertNotContains(password_change, 'Your password must contain at least 8 characters')

    def test_password_reset_flow_sends_and_accepts_one_time_link(self):
        self.client.logout()
        with self.settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            response = self.client.get(reverse('password_reset'))
            self.assertEqual(response.status_code, 200)
            response = self.client.post(reverse('password_reset'), {'email': self.alice.email})
            self.assertRedirects(response, reverse('password_reset_done'))
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, [self.alice.email])
            self.assertEqual(mail.outbox[0].from_email, 'LOVENY Support <help.hoxobil@gmail.com>')
            self.assertEqual(len(mail.outbox[0].alternatives), 1)
            self.assertEqual(mail.outbox[0].alternatives[0][1], 'text/html')
            self.assertIn('Reset My Password', mail.outbox[0].alternatives[0][0])

            reset_url = next(
                line.strip()
                for line in mail.outbox[0].body.splitlines()
                if '/accounts/reset/' in line
            )
            response = self.client.get(reset_url, follow=True)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Choose a new password')
            reset_url = response.request['PATH_INFO']
            response = self.client.post(reset_url, {
                'new_password1': 'different-secure-password-456',
                'new_password2': 'different-secure-password-456',
            })
            self.assertRedirects(response, reverse('password_reset_complete'))
            self.alice.refresh_from_db()
            self.assertTrue(self.alice.check_password('different-secure-password-456'))
            expired_response = self.client.get(reset_url, follow=True)
            self.assertEqual(expired_response.status_code, 200)
            self.assertContains(expired_response, 'This reset link has expired')

    def test_password_reset_alias_and_smtp_error_handling(self):
        self.client.logout()
        response = self.client.get(reverse('password_reset_alias'))
        self.assertEqual(response.status_code, 200)

        import smtplib
        with patch('django.core.mail.backends.locmem.EmailBackend.send_messages', side_effect=smtplib.SMTPConnectError(421, b'Cannot connect')):
            with self.settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
                response = self.client.post(reverse('password_reset'), {'email': self.alice.email})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Unable to deliver the reset email due to a mail server connection issue.')

    def test_direct_interest_creates_match_after_reciprocal_like(self):
        self.client.force_login(self.alice)
        response = self.post_json(reverse('swipe_action'), {
            'target_user_id': self.bob.pk,
            'action': 'DIRECT',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['is_match'])
        self.assertTrue(Swipe.objects.get(swiper=self.alice, swiped=self.bob).is_direct)

        self.client.force_login(self.bob)
        response = self.post_json(reverse('swipe_action'), {
            'target_user_id': self.alice.pk,
            'action': 'LIKE',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['is_match'])
        match = Match.objects.get()
        self.assertTrue(match.has_direct_interest)
        self.assertEqual(match.mode, 'DATING')
        match_page = self.client.get(reverse('match_list'))
        self.assertContains(match_page, 'Open private chat')
        self.assertNotContains(match_page, 'Open WhatsApp')
        self.assertNotContains(match_page, self.bob_profile.whatsapp_number)

    def test_swipe_rejects_self_repeat_and_invalid_payload(self):
        self.client.force_login(self.alice)
        response = self.post_json(reverse('swipe_action'), {
            'target_user_id': self.alice.pk,
            'action': 'LIKE',
        })
        self.assertEqual(response.status_code, 400)

        response = self.post_json(reverse('swipe_action'), {
            'target_user_id': self.bob.pk,
            'action': 'PASS',
        })
        self.assertEqual(response.status_code, 200)
        response = self.post_json(reverse('swipe_action'), {
            'target_user_id': self.bob.pk,
            'action': 'LIKE',
        })
        self.assertEqual(response.status_code, 409)
        response = self.client.post(reverse('swipe_action'), '{', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_swipe_api_and_page_support_empty_feed_fallback(self):
        self.client.force_login(self.alice)
        Swipe.objects.create(swiper=self.alice, swiped=self.bob, type='PASS')
        response = self.client.get(reverse('get_profiles_json'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['profiles'], [])

        response = self.client.get(reverse('swipe_card'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You're all caught up")

    def test_profile_settings_and_chat_templates_render(self):
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(reverse('index')), 'Sex Call')
        profile_page = self.client.get(reverse('profile'))
        self.assertEqual(profile_page.status_code, 200)
        self.assertContains(profile_page, 'max-w-md')
        self.assertContains(profile_page, 'profile-hero')
        self.assertContains(profile_page, 'profile-sheet')
        self.assertContains(profile_page, 'Edit profile')
        self.assertContains(profile_page, 'Online')
        self.assertContains(profile_page, 'backdrop-filter: blur(16px)')
        self.assertEqual(self.client.get(reverse('edit_profile')).status_code, 200)
        self.assertNotContains(self.client.get(reverse('edit_profile')), 'Connection mode')
        self.assertNotContains(self.client.get(reverse('edit_profile')), 'WhatsApp')
        self.assertContains(self.client.get(reverse('edit_profile')), 'Latitude (decimal degrees)')
        settings_page = self.client.get(reverse('settings'))
        self.assertEqual(settings_page.status_code, 200)
        self.assertContains(settings_page, 'Sex Call')
        self.assertContains(settings_page, 'toggle-row')
        self.assertNotContains(settings_page, 'Sugar')
        self.assertNotContains(settings_page, 'Arrangement')
        premium_page = self.client.get(reverse('premium_landing'))
        self.assertEqual(premium_page.status_code, 200)
        self.assertContains(premium_page, 'Choose your Dating Pass')
        self.assertContains(premium_page, 'Unlock unlimited features, priority matching, and instant access for your chosen mode.')
        self.assertNotContains(premium_page, 'category-tab')
        self.assertNotContains(premium_page, 'Choose your Hookup Pass')
        self.assertContains(premium_page, 'viewport-fit=cover')
        self.assertContains(premium_page, 'bottom-nav-container')
        self.assertEqual(
            premium_page.context['plans_api_url'],
            reverse('subscription_plans_api'),
        )
        self.assertEqual(self.client.get(reverse('public_profile', args=[self.bob.pk])).status_code, 200)
        dating_plan = SubscriptionPlan.objects.get(category='DATING', duration_days=3)
        checkout = self.client.get(
            reverse('premium_checkout'),
            {'category': 'DATING', 'plan_id': dating_plan.pk},
        )
        self.assertEqual(checkout.status_code, 200)
        self.assertContains(checkout, 'Dating Premium')
        self.assertContains(checkout, '₦200')
        self.assertContains(checkout, 'Silver')
        self.assertContains(checkout, '3 days of access')
        hookup_checkout = self.client.get(
            reverse('premium_checkout'),
            {
                'category': 'HOOKUP',
                'plan_id': SubscriptionPlan.objects.get(
                    category='HOOKUP',
                    duration_days=3,
                ).pk,
            },
        )
        self.assertEqual(hookup_checkout.status_code, 400)
        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.save(update_fields=['relationship_mode'])
        hookup_checkout = self.client.get(reverse('premium_checkout'), {
            'category': 'DATING',
            'plan_id': SubscriptionPlan.objects.get(category='HOOKUP', duration_days=3).pk,
        })
        self.assertEqual(hookup_checkout.status_code, 200)
        self.assertContains(hookup_checkout, 'Hookup Premium')
        self.assertContains(hookup_checkout, '₦300')

        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(self.client.get(reverse('chat_room', args=[match.pk])).status_code, 200)

    def test_support_page_offers_whatsapp_as_the_only_support_channel(self):
        response = self.client.get(reverse('contact'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'https://wa.me/2349130273282?text=Hello%20Loveny%20Support,%20I%20need%20help%20with%20my%20account',
        )
        self.assertContains(response, 'target="_blank" rel="noopener noreferrer"')
        self.assertContains(response, 'Chat on WhatsApp')
        self.assertContains(response, 'Having trouble with your account or payment? Our support team in Lagos is ready to assist.')
        self.assertNotContains(response, 'Email Support')
        self.assertNotContains(response, 'mailto:')

        newcomer = User.objects.create_user(
            username='newcomer',
            email='newcomer@example.com',
            password='test-password-123',
        )
        self.client.force_login(newcomer)
        response = self.client.get(reverse('create_profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Connection Style')
        self.assertContains(response, 'Sex Call')

    def test_likes_are_hidden_until_premium(self):
        Swipe.objects.create(swiper=self.bob, swiped=self.alice, type='LIKE')
        self.client.force_login(self.alice)
        response = self.client.get(reverse('likes_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Likes are a Premium feature')
        self.assertNotContains(response, 'bob,')

        self.alice_profile.dating_premium_expiry = timezone.now() + timedelta(days=1)
        self.alice_profile.dating_premium_tier = 'SILVER'
        self.alice_profile.save()
        response = self.client.get(reverse('likes_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'bob')

    def test_chat_requires_active_match_and_respects_privacy(self):
        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=2),
        )
        self.client.force_login(self.alice)
        room_response = self.client.get(reverse('chat_room', args=[match.pk]))
        self.assertEqual(room_response.status_code, 200)
        conversation = room_response.context['conversation']
        self.assertEqual(conversation.participants.count(), 2)

        send_url = reverse('conversation_send_api', args=[conversation.pk])
        messages_url = reverse('conversation_messages_api', args=[conversation.pk])
        response = self.post_json(send_url, {'text': 'Hello'})
        self.assertEqual(response.status_code, 201)
        message = ChatMessage.objects.get()
        self.assertEqual(message.sender, self.alice)
        self.assertEqual(message.conversation, conversation)
        self.assertEqual(message.text, 'Hello')
        self.assertIn(self.alice, message.read_by.all())

        response = self.client.get(messages_url)
        self.assertEqual(response.json()['messages'][0]['text'], 'Hello')
        self.assertEqual(response.json()['messages'][0]['is_read'], False)
        self.assertEqual(
            self.client.get(messages_url, {'after_id': message.pk}).json()['messages'],
            [],
        )

        response = self.client.get(reverse('conversations_api'))
        self.assertEqual(response.json()['conversations'][0]['unread_count'], 0)
        self.assertEqual(response.json()['conversations'][0]['last_message']['text'], 'Hello')

        self.client.force_login(self.bob)
        response = self.client.get(reverse('conversations_api'))
        self.assertEqual(response.json()['conversations'][0]['unread_count'], 1)
        response = self.client.get(messages_url)
        self.assertEqual(response.status_code, 200)
        message.refresh_from_db()
        self.assertIn(self.bob, message.read_by.all())
        self.assertEqual(response.json()['messages'][0]['is_read'], True)

        response = self.client.get(reverse('conversations_api'))
        self.assertEqual(response.json()['conversations'][0]['unread_count'], 0)

        self.bob_profile.allow_messages = False
        self.bob_profile.save()
        response = self.post_json(send_url, {'text': 'Again'})
        self.assertEqual(response.status_code, 403)

        match.expires_at = timezone.now() - timedelta(seconds=1)
        match.save()
        response = self.client.get(messages_url)
        self.assertEqual(response.status_code, 404)
        response = self.client.get(reverse('chat_room', args=[match.pk]))
        self.assertEqual(response.status_code, 404)

    def test_conversation_endpoints_require_participant_access(self):
        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=2),
        )
        conversation = Conversation.objects.create()
        conversation.participants.add(self.alice, self.bob)
        stranger, _ = self.make_profile('stranger')
        self.client.force_login(stranger)

        self.assertEqual(
            self.client.get(
                reverse('conversation_messages_api', args=[conversation.pk]),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.post_json(
                reverse('conversation_send_api', args=[conversation.pk]),
                {'text': 'Not allowed'},
            ).status_code,
            404,
        )

    def test_payment_requires_server_verification_and_is_idempotent(self):
        self.client.force_login(self.alice)
        plan = SubscriptionPlan.objects.get(category='DATING', duration_days=3)
        plan.price = Decimal('234.50')
        plan.save()
        payload = {
            'plan_id': plan.pk,
            'product': 'DATING',
            'provider': 'paystack',
            'reference': 'paystack-reference-1',
        }
        with patch('dating.views.settings.PAYSTACK_SECRET_KEY', ''):
            response = self.post_json(reverse('verify_payment'), payload)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.alice_profile.is_premium())

        with patch('dating.views._provider_payment', return_value=(True, 23450, 'NGN', self.alice.email)):
            response = self.post_json(reverse('verify_payment'), payload)
            self.assertEqual(response.status_code, 200)
            self.alice_profile.refresh_from_db()
            first_expiry = self.alice_profile.dating_premium_expiry
            self.assertTrue(self.alice_profile.is_premium())
            self.assertFalse(self.alice_profile.is_hookup_premium)
            self.assertEqual(
                self.client.get(reverse('premium_success'), {'product': 'DATING'}).status_code,
                200,
            )

            response = self.post_json(reverse('verify_payment'), payload)
            self.assertTrue(response.json()['already_applied'])
            self.alice_profile.refresh_from_db()
            self.assertEqual(self.alice_profile.dating_premium_expiry, first_expiry)

        self.assertEqual(PaymentTransaction.objects.count(), 1)
        self.assertEqual(PaymentTransaction.objects.get().product, 'DATING')
        self.assertEqual(PaymentTransaction.objects.get().amount_kobo, 23450)

    def test_hookup_sex_call_payment_does_not_extend_dating_access(self):
        self.client.force_login(self.alice)
        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.dating_premium_expiry = timezone.now() + timedelta(days=2)
        self.alice_profile.save()
        plan = SubscriptionPlan.objects.get(category='HOOKUP', duration_days=3)
        payload = {
            'plan_id': plan.pk,
            'product': 'HOOKUP',
            'provider': 'paystack',
            'reference': 'hookup-reference-1',
        }
        with patch('dating.views._provider_payment', return_value=(True, 30000, 'NGN', self.alice.email)):
            response = self.post_json(reverse('verify_payment'), payload)

        self.assertEqual(response.status_code, 200)
        self.alice_profile.refresh_from_db()
        self.assertTrue(self.alice_profile.is_dating_premium)
        self.assertTrue(self.alice_profile.is_hookup_premium)
        self.assertFalse(self.alice_profile.is_sex_call_premium)
        self.assertEqual(self.alice_profile.dating_premium_tier, '')
        self.assertEqual(PaymentTransaction.objects.get().product, 'HOOKUP')

    def test_subscription_plan_api_groups_active_database_plans(self):
        self.client.force_login(self.alice)
        dating_plan = SubscriptionPlan.objects.get(category='DATING', duration_days=7)
        response = self.client.get(reverse('subscription_plans_api'))
        self.assertEqual(response.status_code, 200)
        seeded_prices = {
            plan['duration_days']: plan['price']
            for plan in response.json()['dating']
        }
        self.assertEqual(seeded_prices, {3: '200.00', 7: '1000.00', 30: '3500.00'})

        dating_plan.price = '1234.50'
        dating_plan.save()
        disabled_plan = SubscriptionPlan.objects.get(category='SEX_CALL', duration_days=3)
        disabled_plan.is_active = False
        disabled_plan.save()

        response = self.client.get(reverse('subscription_plans_api'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {'dating'})
        dating_prices = {
            plan['duration_days']: plan['price']
            for plan in payload['dating']
        }
        self.assertEqual(dating_prices, {3: '200.00', 7: '1234.50', 30: '3500.00'})
        self.assertEqual(
            next(plan for plan in payload['dating'] if plan['id'] == dating_plan.pk)['price'],
            '1234.50',
        )
        self.assertTrue(all('features' in plan for plans in payload.values() for plan in plans))
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save(update_fields=['relationship_mode'])
        response = self.client.get(reverse('subscription_plans_api'))
        self.assertEqual(set(response.json()), {'sex_call'})
        # In closed loop economy, sex call has no subscription plans
        self.assertEqual(response.json()['sex_call'], [])

    def test_subscription_checkout_and_payment_are_limited_to_selected_mode(self):
        self.client.force_login(self.alice)
        hookup_plan = SubscriptionPlan.objects.get(category='HOOKUP', duration_days=3)
        self.assertEqual(
            self.client.get(reverse('premium_checkout'), {'plan_id': hookup_plan.pk}).status_code,
            400,
        )
        response = self.post_json(reverse('verify_payment'), {
            'plan_id': hookup_plan.pk,
            'product': 'HOOKUP',
            'provider': 'paystack',
            'reference': 'cross-mode-reference',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['message'], 'plan_mode_mismatch')
        self.assertEqual(PaymentTransaction.objects.count(), 0)

        response = self.client.get(reverse('subscription_plans_api'))
        self.assertEqual(set(response.json()), {'dating'})
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save(update_fields=['relationship_mode'])
        response = self.client.get(reverse('premium_landing'))
        self.assertRedirects(response, reverse('sex_call_hub'))
        response = self.client.get(reverse('subscription_plans_api'))
        self.assertEqual(set(response.json()), {'sex_call'})
        self.assertEqual(response.json()['sex_call'], [])

    def test_calls_require_sex_call_access_active_match_and_participant_signaling(self):
        Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            mode='SEX_CALL',
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.client.force_login(self.alice)
        initiate_url = reverse('call_initiate_api')
        # Requires coins to call: Alice has 0 coins, requires call_rate_per_minute (default 20)
        self.alice_profile.coin_balance = 0
        self.alice_profile.save(update_fields=['coin_balance'])
        response = self.post_json(initiate_url, {'receiver_id': self.bob.pk})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['message'], 'insufficient_coins')

        # Fund Alice with coins
        self.alice_profile.coin_balance = 50
        self.alice_profile.save(update_fields=['coin_balance'])
        matches_page = self.client.get(reverse('match_list'))
        self.assertContains(matches_page, 'Start video call')
        self.assertContains(matches_page, 'incoming-call-sheet')
        self.assertContains(matches_page, 'video-calls.js')
        response = self.post_json(initiate_url, {'receiver_id': self.bob.pk})
        self.assertEqual(response.status_code, 201)
        call = CallSession.objects.get()
        self.assertEqual(call.status, 'ringing')
        self.assertEqual(call.caller, self.alice)
        self.assertEqual(call.receiver, self.bob)
        room_id = call.room_id

        self.client.force_login(self.bob)
        self.assertEqual(
            self.client.get(reverse('incoming_calls_api')).json()['call']['room_id'],
            str(room_id),
        )
        response = self.post_json(reverse('call_respond_api', args=[room_id]), {'action': 'accept'})
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'connected')
        self.assertIsNotNone(call.started_at)

        signals_url = reverse('call_signals_api', args=[room_id])
        send_signal_url = reverse('call_signal_send_api', args=[room_id])
        stranger, _ = self.make_profile('call-stranger')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(signals_url).status_code, 404)

        self.client.force_login(self.alice)
        response = self.post_json(send_signal_url, {
            'type': 'offer',
            'payload': {'type': 'offer', 'sdp': 'fake-offer'},
        })
        self.assertEqual(response.status_code, 201)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(signals_url).json()['signals'][0]['type'], 'offer')
        response = self.post_json(send_signal_url, {
            'type': 'answer',
            'payload': {'type': 'answer', 'sdp': 'fake-answer'},
        })
        self.assertEqual(response.status_code, 201)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(signals_url).json()['signals'][0]['type'], 'answer')
        response = self.client.post(reverse('call_end_api', args=[room_id]))
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.assertIsNotNone(call.ended_at)

        expired_match = Match.objects.get(user1=self.alice, user2=self.bob)
        expired_match.expires_at = timezone.now() - timedelta(seconds=1)
        expired_match.save(update_fields=['expires_at'])
        response = self.post_json(initiate_url, {'receiver_id': self.bob.pk})
        self.assertEqual(response.status_code, 403)

        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.sex_call_premium_expiry = None
        self.alice_profile.coin_balance = 0
        self.alice_profile.save(update_fields=['relationship_mode', 'sex_call_premium_expiry', 'coin_balance'])
        expired_match.expires_at = timezone.now() + timedelta(days=1)
        expired_match.save(update_fields=['expires_at'])
        response = self.post_json(initiate_url, {'receiver_id': self.bob.pk})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['message'], 'insufficient_coins')

        # Now fund Alice with 20 coins
        self.alice_profile.coin_balance = 20
        self.alice_profile.save(update_fields=['coin_balance'])
        response = self.post_json(initiate_url, {'receiver_id': self.bob.pk})
        self.assertEqual(response.status_code, 201)

    def test_subscription_pass_entitlements_are_category_specific(self):
        self.alice_profile.hookup_premium_expiry = timezone.now() + timedelta(days=1)
        self.alice_profile.hookup_premium_tier = 'Silver'
        self.alice_profile.save()

        self.assertTrue(self.alice_profile.is_premium('HOOKUP'))
        self.assertFalse(self.alice_profile.is_premium('SEX_CALL'))

    def test_profile_admin_changelist_loads_reversion_template(self):
        admin_user = User.objects.create_superuser(
            username='admin-user',
            email='admin@example.com',
            password='admin-password-123',
        )
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin:dating_profile_changelist'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'reversion/change_list.html')

    def test_payment_provider_response_is_verified_server_side(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps({
                    'status': True,
                    'data': {
                        'status': 'success',
                        'reference': 'verified-ref',
                        'amount': 20000,
                        'currency': 'NGN',
                        'customer': {'email': self_email},
                    },
                }).encode()

        self_email = self.alice.email
        with (
            patch('dating.views.settings.PAYSTACK_SECRET_KEY', 'server-test-key'),
            patch('dating.views.urlopen', return_value=FakeResponse()) as mocked_urlopen,
        ):
            payment = _provider_payment('paystack', 'verified-ref')

        self.assertEqual(payment, (True, 20000, 'NGN', self.alice.email))
        request = mocked_urlopen.call_args.args[0]
        self.assertIn('/transaction/verify/verified-ref', request.full_url)
        self.assertEqual(request.get_header('Authorization'), 'Bearer server-test-key')

    def test_wallet_balance_api_and_welcome_bonus(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse('wallet_balance_api'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['coin_balance'], 30)

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 30)
        self.assertTrue(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='WELCOME_BONUS',
            ).exists()
        )

        # Subsequent call does not grant duplicate welcome bonus
        response2 = self.client.get(reverse('wallet_balance_api'))
        self.assertEqual(response2.json()['coin_balance'], 30)
        self.assertEqual(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='WELCOME_BONUS',
            ).count(),
            1,
        )

    def test_coin_packages_api(self):
        package = CoinPackage.objects.create(
            name='Test Starter',
            coins=100,
            bonus_coins=20,
            price=Decimal('1500.00'),
            is_popular=True,
            order=1,
        )
        response = self.client.get(reverse('coin_packages_api'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertTrue(len(data['packages']) >= 1)
        found = next((p for p in data['packages'] if p['id'] == package.pk), None)
        self.assertIsNotNone(found)
        self.assertEqual(found['total_coins'], 120)

    def test_verify_payment_for_coins(self):
        self.client.force_login(self.alice)
        package = CoinPackage.objects.create(
            name='100 Coins Pack',
            coins=100,
            bonus_coins=10,
            price=Decimal('1500.00'),
            order=1,
        )
        self.alice_profile.coin_balance = 0
        self.alice_profile.save(update_fields=['coin_balance'])

        with patch('dating.views._provider_payment', return_value=(True, 150000, 'NGN', self.alice.email)):
            response = self.post_json(reverse('verify_payment'), {
                'product': 'COINS',
                'package_id': package.pk,
                'provider': 'paystack',
                'reference': 'ref-coin-test-123',
            })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['added_coins'], 110)
        self.assertEqual(data['coin_balance'], 110)

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 110)
        self.assertTrue(
            PaymentTransaction.objects.filter(reference='ref-coin-test-123', product='COINS').exists()
        )
        self.assertTrue(
            CoinTransaction.objects.filter(user=self.alice, transaction_type='PURCHASE', amount=110).exists()
        )

    def test_call_billing_grace_period_under_20_seconds(self):
        Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 100
        self.alice_profile.save(update_fields=['relationship_mode', 'coin_balance'])

        self.client.force_login(self.alice)
        init_res = self.post_json(reverse('call_initiate_api'), {'receiver_id': self.bob.pk})
        self.assertEqual(init_res.status_code, 201)
        room_id = init_res.json()['call']['room_id']

        self.client.force_login(self.bob)
        resp_res = self.post_json(reverse('call_respond_api', args=[room_id]), {'action': 'accept'})
        self.assertEqual(resp_res.status_code, 200)

        call = CallSession.objects.get(room_id=room_id)
        call.started_at = timezone.now() - timedelta(seconds=12)
        call.save(update_fields=['started_at'])

        self.client.force_login(self.alice)
        end_res = self.post_json(reverse('call_end_api', args=[room_id]), {})
        self.assertEqual(end_res.status_code, 200)
        end_data = end_res.json()
        self.assertTrue(end_data['call']['grace_period_applied'])
        self.assertEqual(end_data['call']['coins_spent'], 0)

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 100)
        self.assertTrue(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='CALL_REFUND',
                call=call,
            ).exists()
        )

    def test_call_billing_over_20_seconds_and_diamond_earning(self):
        Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 100
        self.alice_profile.save(update_fields=['relationship_mode', 'coin_balance'])
        self.bob_profile.earned_diamonds = 0
        self.bob_profile.save(update_fields=['earned_diamonds'])

        self.client.force_login(self.alice)
        init_res = self.post_json(reverse('call_initiate_api'), {'receiver_id': self.bob.pk})
        room_id = init_res.json()['call']['room_id']

        self.client.force_login(self.bob)
        self.post_json(reverse('call_respond_api', args=[room_id]), {'action': 'accept'})

        call = CallSession.objects.get(room_id=room_id)
        # 95 seconds = 20s grace + 75s billable (2 minutes * 20 = 40 coins)
        call.started_at = timezone.now() - timedelta(seconds=95)
        call.save(update_fields=['started_at'])

        self.client.force_login(self.alice)
        end_res = self.post_json(reverse('call_end_api', args=[room_id]), {})
        self.assertEqual(end_res.status_code, 200)
        end_data = end_res.json()
        self.assertFalse(end_data['call']['grace_period_applied'])
        self.assertEqual(end_data['call']['coins_spent'], 40)

        self.alice_profile.refresh_from_db()
        self.bob_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 60)
        # 70% of 40 coins = 28 diamonds
        self.assertEqual(self.bob_profile.earned_diamonds, 28)

        self.assertTrue(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='CALL_DEDUCTION',
                amount=-40,
                call=call,
            ).exists()
        )
        self.assertTrue(
            CoinTransaction.objects.filter(
                user=self.bob,
                transaction_type='CALL_EARNING',
                amount=28,
                call=call,
            ).exists()
        )

    def test_call_in_call_gifting_and_diamond_conversion(self):
        Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 100
        self.alice_profile.save(update_fields=['relationship_mode', 'coin_balance'])
        self.bob_profile.earned_diamonds = 0
        self.bob_profile.save(update_fields=['earned_diamonds'])

        self.client.force_login(self.alice)
        init_res = self.post_json(reverse('call_initiate_api'), {'receiver_id': self.bob.pk})
        room_id = init_res.json()['call']['room_id']

        self.client.force_login(self.bob)
        self.post_json(reverse('call_respond_api', args=[room_id]), {'action': 'accept'})

        # Alice sends champagne gift (50 coins)
        self.client.force_login(self.alice)
        gift_res = self.post_json(reverse('call_gift_api', args=[room_id]), {'gift_type': 'champagne'})
        self.assertEqual(gift_res.status_code, 200)
        gift_data = gift_res.json()
        self.assertEqual(gift_data['status'], 'success')
        self.assertEqual(gift_data['caller_coins'], 50)
        # 70% of 50 = 35 diamonds
        self.assertEqual(gift_data['diamond_award'], 35)

        self.alice_profile.refresh_from_db()
        self.bob_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 50)
        self.assertEqual(self.bob_profile.earned_diamonds, 35)

        self.assertTrue(
            CallGift.objects.filter(
                sender=self.alice,
                receiver=self.bob,
                gift_type='champagne',
                coins_cost=50,
            ).exists()
        )

    def test_call_heartbeat_auto_ends_when_coins_depleted(self):
        Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 20
        self.alice_profile.save(update_fields=['relationship_mode', 'coin_balance'])

        self.client.force_login(self.alice)
        init_res = self.post_json(reverse('call_initiate_api'), {'receiver_id': self.bob.pk})
        room_id = init_res.json()['call']['room_id']

        self.client.force_login(self.bob)
        self.post_json(reverse('call_respond_api', args=[room_id]), {'action': 'accept'})

        call = CallSession.objects.get(room_id=room_id)
        call.started_at = timezone.now() - timedelta(seconds=90)
        call.save(update_fields=['started_at'])

        # Alice heartbeats at 90s, requiring 40 coins, but only has 20
        self.client.force_login(self.alice)
        hb_res = self.post_json(reverse('call_heartbeat_api', args=[room_id]), {'duration_seconds': 90})
        self.assertEqual(hb_res.status_code, 200)
        hb_data = hb_res.json()
        self.assertEqual(hb_data['call']['status'], 'ended')
        self.assertEqual(hb_data['reason'], 'coins_exhausted')

        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 0)

    def test_site_configuration_dynamic_rates_and_singleton(self):
        cfg = SiteConfiguration.get_solo()
        cfg.call_rate_per_minute = 35
        cfg.grace_period_seconds = 15
        cfg.host_commission_percentage = 80
        cfg.welcome_bonus_coins = 50
        cfg.announcement_banner = "Test Site Announcement"
        cfg.is_announcement_active = True
        cfg.save()

        res = self.client.get(reverse('site_config_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['call_rate_per_minute'], 35)
        self.assertEqual(data['grace_period_seconds'], 15)
        self.assertEqual(data['host_commission_percentage'], 80)
        self.assertEqual(data['welcome_bonus_coins'], 50)
        self.assertEqual(data['announcement_banner'], "Test Site Announcement")
        self.assertTrue(data['is_announcement_active'])

        # Singleton enforcement
        cfg2 = SiteConfiguration.objects.create(call_rate_per_minute=99)
        self.assertEqual(cfg2.pk, 1)

    def test_gifts_api_and_dynamic_gift_item(self):
        GiftItem.objects.all().delete()
        g1 = GiftItem.objects.create(name='Magic Wand', slug='wand', icon='🪄', coin_cost=75, order=1)
        g2 = GiftItem.objects.create(name='Golden Castle', slug='castle', icon='🏰', coin_cost=400, order=2)

        res = self.client.get(reverse('gifts_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data['gifts']), 2)
        self.assertEqual(data['gifts'][0]['slug'], 'wand')
        self.assertEqual(data['gifts'][0]['coin_cost'], 75)

        # Alice sends custom gift to Bob during active call
        Match.objects.create(user1=self.alice, user2=self.bob, expires_at=timezone.now() + timedelta(days=1))
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 100
        self.alice_profile.save()

        self.client.force_login(self.alice)
        call_res = self.post_json(reverse('call_initiate_api'), {'receiver_id': self.bob.pk})
        room_id = call_res.json()['call']['room_id']

        self.client.force_login(self.bob)
        self.post_json(reverse('call_respond_api', args=[room_id]), {'action': 'accept'})

        self.client.force_login(self.alice)
        gift_res = self.post_json(reverse('call_gift_api', args=[room_id]), {'gift_type': 'wand'})
        self.assertEqual(gift_res.status_code, 200)
        g_data = gift_res.json()
        self.assertEqual(g_data['gift']['coins'], 75)
        self.assertEqual(g_data['caller_coins'], 25)
        # Commission is 70% of 75 = 52 diamonds
        self.assertEqual(g_data['diamond_award'], 52)

    def test_online_hosts_api(self):
        self.bob_profile.is_host_ready = True
        self.bob_profile.response_rate = 96
        self.bob_profile.total_calls_completed = 14
        self.bob_profile.save()

        res = self.client.get(reverse('online_hosts_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertTrue(len(data['hosts']) >= 1)
        bob_item = next(h for h in data['hosts'] if h['username'] == 'bob')
        self.assertTrue(bob_item['is_host_ready'])
        self.assertEqual(bob_item['response_rate'], 96)
        self.assertEqual(bob_item['total_calls'], 14)

    def test_quick_call_match_api(self):
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 50
        self.alice_profile.save()

        self.bob_profile.is_host_ready = True
        self.bob_profile.save()

        self.client.force_login(self.alice)
        res = self.client.post(reverse('quick_call_match_api'), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertTrue('room_id' in data)
        self.assertEqual(data['call']['receiver_name'], 'bob')
        self.assertEqual(data['host']['username'], 'bob')

    def test_diamond_to_coin_conversion_flow(self):
        cfg = SiteConfiguration.get_solo()
        cfg.diamond_to_coin_percentage = 70
        cfg.save()

        self.bob_profile.earned_diamonds = 250
        self.bob_profile.coin_balance = 10
        self.bob_profile.save()

        self.client.force_login(self.bob)

        # Try 0 diamonds
        res_fail = self.post_json(reverse('convert_diamonds_api'), {'diamonds': 0})
        self.assertEqual(res_fail.status_code, 400)

        # Try exceeding balance
        res_fail2 = self.post_json(reverse('convert_diamonds_api'), {'diamonds': 500})
        self.assertEqual(res_fail2.status_code, 400)
        self.assertEqual(res_fail2.json()['error'], 'insufficient_diamonds')

        # Valid conversion: 100 diamonds * 70% = 70 coins
        res_ok = self.post_json(reverse('convert_diamonds_api'), {'diamonds': 100})
        self.assertEqual(res_ok.status_code, 200)
        data = res_ok.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['diamonds_deducted'], 100)
        self.assertEqual(data['coins_credited'], 70)
        self.assertEqual(data['remaining_diamonds'], 150)
        self.assertEqual(data['new_coin_balance'], 80)

        self.bob_profile.refresh_from_db()
        self.assertEqual(self.bob_profile.earned_diamonds, 150)
        self.assertEqual(self.bob_profile.coin_balance, 80)
        self.assertTrue(
            CoinTransaction.objects.filter(
                user=self.bob,
                transaction_type='DIAMOND_CONVERSION',
                amount=70,
            ).exists()
        )

        # Cash withdrawal endpoint returns 410 Gone indicating closed loop economy
        res_with = self.post_json(reverse('withdrawal_request_api'), {'diamonds': 50})
        self.assertEqual(res_with.status_code, 410)
        self.assertEqual(res_with.json()['notice'], 'closed_loop_economy')

    def test_wallet_history_api(self):
        CoinTransaction.objects.create(
            user=self.bob,
            amount=50,
            transaction_type='WELCOME_BONUS',
            description='Test bonus',
        )

        self.client.force_login(self.bob)
        res = self.client.get(reverse('wallet_history_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertTrue(len(data['transactions']) >= 1)
        self.assertEqual(data['withdrawals'], [])

    def test_coin_wallet_model_and_transaction_fields(self):
        wallet = CoinWallet.objects.create(user=self.alice, coin_balance=150)
        self.assertEqual(wallet.coin_balance, 150)
        self.assertEqual(str(wallet), "alice's Wallet: 150 coins")

        tx = CoinTransaction.objects.create(
            user=self.alice,
            sender=self.alice,
            recipient=self.bob,
            amount=-25,
            gift_type='cocktail',
            transaction_type='GIFT_SENT',
            description='Sent cocktail to bob',
        )
        self.assertEqual(tx.sender, self.alice)
        self.assertEqual(tx.recipient, self.bob)
        self.assertEqual(tx.gift_type, 'cocktail')
        self.assertEqual(tx.timestamp, tx.created_at)

    def test_chat_send_gift_api(self):
        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=2),
        )
        self.alice_profile.coin_balance = 200
        self.alice_profile.save()
        self.bob_profile.earned_diamonds = 0
        self.bob_profile.save()

        self.client.force_login(self.alice)
        payload = {
            'match_id': match.pk,
            'gift_type': 'rose',
        }
        res = self.post_json(reverse('chat_send_gift_api'), payload)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data['status'], 'success')

        gift_item = GiftItem.objects.filter(slug='rose').first()
        rose_cost = gift_item.coin_cost if gift_item else 10
        expected_balance = 200 - rose_cost
        expected_diamonds = int(Decimal(rose_cost) * Decimal('0.7'))

        self.assertEqual(data['sender_balance'], expected_balance)
        self.assertEqual(data['gift']['coins'], rose_cost)
        self.assertEqual(data['message']['message_type'], 'gift')
        self.assertEqual(data['message']['metadata']['gift_type'], 'rose')

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, expected_balance)

        self.bob_profile.refresh_from_db()
        self.assertEqual(self.bob_profile.earned_diamonds, expected_diamonds)

        self.assertTrue(
            CoinTransaction.objects.filter(
                sender=self.alice,
                recipient=self.bob,
                gift_type='rose',
                transaction_type='GIFT_SENT',
            ).exists()
        )
        self.assertTrue(
            CoinTransaction.objects.filter(
                sender=self.alice,
                recipient=self.bob,
                gift_type='rose',
                transaction_type='GIFT_RECEIVED',
            ).exists()
        )

    def test_chat_send_gift_insufficient_coins(self):
        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=2),
        )
        self.alice_profile.coin_balance = 1 # lower than any gift cost (rose is 5)
        self.alice_profile.save()

        self.client.force_login(self.alice)
        payload = {
            'match_id': match.pk,
            'gift_type': 'rose',
        }
        res = self.post_json(reverse('chat_send_gift_api'), payload)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['message'], 'insufficient_coins')

    def test_chat_stickers_and_rich_messaging(self):
        res_stickers = self.client.get(reverse('chat_stickers_api'))
        self.assertEqual(res_stickers.status_code, 200)
        data = res_stickers.json()
        self.assertEqual(data['status'], 'success')
        self.assertTrue(len(data['packs']) >= 4)
        self.assertEqual(data['packs'][0]['id'], 'flirty')

        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=2),
        )
        conv = Conversation.objects.create()
        conv.participants.add(self.alice, self.bob)

        self.client.force_login(self.alice)
        res_send = self.post_json(
            reverse('conversation_send_api', args=[conv.pk]),
            {
                'text': '💋',
                'message_type': 'sticker',
                'metadata': {'icon': '💋', 'name': 'Kiss', 'caption': 'Smooch!'},
            }
        )
        self.assertEqual(res_send.status_code, 201)
        msg_data = res_send.json()['message']
        self.assertEqual(msg_data['message_type'], 'sticker')
        self.assertEqual(msg_data['metadata']['caption'], 'Smooch!')

        res_list = self.client.get(reverse('conversation_messages_api', args=[conv.pk]))
        self.assertEqual(res_list.status_code, 200)
        messages = res_list.json()['messages']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['message_type'], 'sticker')
        self.assertEqual(messages[0]['metadata']['icon'], '💋')

    def test_sex_call_hub_page_and_hosts_api(self):
        self.client.force_login(self.alice)
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 200
        self.alice_profile.earned_diamonds = 80
        self.alice_profile.save()

        # Sex Call Hub page renders with 200 and expected components
        response = self.client.get(reverse('sex_call_hub'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Hot')
        self.assertContains(response, 'Nearby')
        self.assertContains(response, 'cosmic-match-overlay')
        self.assertContains(response, 'diamond-exchange-modal')
        self.assertContains(response, '200') # coin balance
        self.assertContains(response, '80') # diamond balance

        # Hosts API
        api_res = self.client.get(reverse('sex_call_hosts_api'), {'tab': 'hot'})
        self.assertEqual(api_res.status_code, 200)
        data = api_res.json()
        self.assertTrue(data['success'])
        self.assertIn('hosts', data)
        self.assertEqual(data['tab'], 'hot')

        # Nearby tab
        api_res_nearby = self.client.get(reverse('sex_call_hosts_api'), {'tab': 'nearby'})
        self.assertEqual(api_res_nearby.status_code, 200)
        self.assertEqual(api_res_nearby.json()['tab'], 'nearby')

    def test_call_room_page_access_and_controls(self):
        call = CallSession.objects.create(
            caller=self.alice,
            receiver=self.bob,
            status='accepted',
        )

        # Alice (caller) can access call room
        self.client.force_login(self.alice)
        res_alice = self.client.get(reverse('call_room_page', args=[call.room_id]))
        self.assertEqual(res_alice.status_code, 200)
        self.assertContains(res_alice, 'floating-chat-container')
        self.assertContains(res_alice, 'local-video')
        self.assertContains(res_alice, 'remote-video')

        # Bob (receiver) can access call room
        self.client.force_login(self.bob)
        res_bob = self.client.get(reverse('call_room_page', args=[call.room_id]))
        self.assertEqual(res_bob.status_code, 200)

        # Charlie (third party) is forbidden
        charlie_user, charlie_profile = self.make_profile('charlie', mode='SEX_CALL')
        self.client.force_login(charlie_user)
        res_charlie = self.client.get(reverse('call_room_page', args=[call.room_id]))
        self.assertEqual(res_charlie.status_code, 403)

    def test_preview_test_profiles_visibility_in_discovery_and_sex_call(self):
        test_user, test_profile = self.make_profile('test_model_1', mode='SEX_CALL')
        test_profile.is_test_profile = True
        test_profile.show_in_discovery = False
        test_profile.is_host_ready = True
        test_user.is_active = False
        test_user.save()
        test_profile.save()

        # Standard non-staff user does not see test profile
        self.client.force_login(self.alice)
        res_normal = self.client.get(reverse('get_profiles_json'))
        self.assertEqual(res_normal.status_code, 200)
        self.assertFalse(any(p['id'] == test_profile.pk for p in res_normal.json()['profiles']))

        # Staff user enables preview_test_profiles
        staff_user, staff_profile = self.make_profile('staff_member', mode='SEX_CALL')
        staff_user.is_staff = True
        staff_user.save()
        self.client.force_login(staff_user)

        session = self.client.session
        session['preview_test_profiles'] = True
        session.save()

        # In discovery JSON, test profile now appears
        res_staff = self.client.get(reverse('get_profiles_json'))
        self.assertEqual(res_staff.status_code, 200)
        profiles = res_staff.json()['profiles']
        self.assertTrue(any(p['id'] == test_profile.pk for p in profiles))

        # In sex call hosts API, test profile appears for staff preview
        res_hosts = self.client.get(reverse('sex_call_hosts_api'), {'tab': 'hot'})
        self.assertEqual(res_hosts.status_code, 200)
        hosts = res_hosts.json()['hosts']
        self.assertTrue(any(h['id'] == test_profile.pk for h in hosts))

    def test_coin_checkout_page_renders_packages_and_closed_loop_rules(self):
        self.client.force_login(self.alice)
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 45
        self.alice_profile.earned_diamonds = 30
        self.alice_profile.save()

        # Direct access to checkout with plan=1&mode=COINS
        res = self.client.get(reverse('premium_checkout'), {'plan': 1, 'mode': 'COINS'})
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Buy Coins')
        self.assertContains(res, 'Strictly Closed-Loop Economy')
        self.assertContains(res, 'Pay with Paystack')
        self.assertContains(res, '45') # coin balance
        self.assertContains(res, '30') # diamond balance

        # coin_shop named route
        res_shop = self.client.get(reverse('coin_shop'))
        self.assertEqual(res_shop.status_code, 200)
        self.assertContains(res_shop, 'Official Sex Call Token Shop')

        # premium_landing with mode=COINS redirects Sex Call users directly to Coin Shop
        res_landing = self.client.get(reverse('premium_landing'), {'mode': 'COINS'})
        self.assertRedirects(res_landing, f"{reverse('premium_checkout')}?mode=COINS")

    def test_dual_discovery_modes_on_sex_call_hub(self):
        self.client.force_login(self.alice)
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save()

        # Sex call hub renders with both Swipe and Grid view controls
        res = self.client.get(reverse('sex_call_hub'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'view-mode-swipe')
        self.assertContains(res, 'view-mode-grid')
        self.assertContains(res, 'grid-view-section')
        self.assertContains(res, 'swipe-view-section')
        self.assertContains(res, 'Hot')
        self.assertContains(res, 'Nearby')

        # Requesting view=swipe explicitly
        res_swipe = self.client.get(reverse('sex_call_hub'), {'view': 'swipe'})
        self.assertEqual(res_swipe.status_code, 200)
        self.assertEqual(res_swipe.context['initial_view'], 'swipe')

    def test_preview_test_profiles_via_query_param_populates_swipe_and_grid(self):
        test_user, test_profile = self.make_profile('test_model_dual', mode='SEX_CALL')
        test_profile.is_test_profile = True
        test_profile.show_in_discovery = False
        test_profile.save()

        self.client.force_login(self.alice)
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save()

        # Swipe deck feed with preview_test_profiles=on
        res_swipe = self.client.get(reverse('get_profiles_json'), {'preview_test_profiles': 'on'})
        self.assertEqual(res_swipe.status_code, 200)
        swipe_profiles = res_swipe.json()['profiles']
        self.assertTrue(any(p['id'] == test_profile.pk for p in swipe_profiles))

        # Grid view feed with preview_test_profiles=on
        res_grid = self.client.get(reverse('sex_call_hosts_api'), {'preview_test_profiles': 'on'})
        self.assertEqual(res_grid.status_code, 200)
        grid_hosts = res_grid.json()['hosts']
        self.assertTrue(any(h['id'] == test_profile.pk for h in grid_hosts))



