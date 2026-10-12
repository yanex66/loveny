import json
import os
import tempfile
from io import StringIO
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django import forms
from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.db.models import Q
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import ProfileAdminForm
from .forms import ProfileCreationForm, ProfileForm, SettingsForm
from .models import (
    CallGift,
    CallSession,
    CallSignal,
    ChatMessage,
    CoinPackage,
    CoinTransaction,
    CoinWallet,
    Conversation,
    GiftItem,
    HookupMatch,
    HookupMessage,
    Match,
    ModeSwitchPaymentIntent,
    PaymentTransaction,
    Profile,
    ProfilePhoto,
    Referral,
    SiteConfiguration,
    SexCallQueue,
    SubscriptionPlan,
    Swipe,
    Tag,
    SupportKnowledgeBase,
    SupportTicket,
    UserReport,
)
from .mode_admin.dating_profiles.models import DatingProfile
from .mode_admin.hookup_profiles.models import HookupProfile
from .mode_admin.sex_call_profiles.models import SexCallProfile
from .services.content_filter import ContentFilter
from .services.support_bot import process_support_inquiry, FALLBACK_RESPONSE
from .views import _award_referral_signup_rewards, _provider_payment, get_profile_batch


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

    def test_admin_provisioning_requires_a_nonempty_environment_password(self):
        from create_admin import main as create_admin

        error = (
            'FATAL: ADMIN_PASSWORD environment variable is not configured. '
            'Aborting admin provisioning.'
        )
        for password in (None, ''):
            with self.subTest(password_is_set=password is not None):
                with patch.dict(os.environ):
                    if password is None:
                        os.environ.pop('ADMIN_PASSWORD', None)
                    else:
                        os.environ['ADMIN_PASSWORD'] = password
                    with self.assertRaisesRegex(SystemExit, error):
                        create_admin()

    def test_admin_login_replaces_frontend_session_and_rejects_regular_credentials(self):
        self.client.force_login(self.alice)

        login_page = self.client.get('/admin/', follow=True)
        self.assertEqual(login_page.status_code, 200)
        self.assertContains(login_page, 'name="username"')
        self.assertNotContains(login_page, 'You are authenticated as')
        self.assertNotIn('_auth_user_id', self.client.session)

        self.client.force_login(self.alice)
        login_response = self.client.post('/admin/login/', {
            'username': self.alice.username,
            'password': 'test-password-123',
            'next': '/admin/',
        })
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(
            login_response,
            'Please enter a correct administrative username and password.',
        )
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_admin_credentials_log_in_from_an_existing_frontend_session(self):
        admin_user = User.objects.create_user(
            username='dedicated-admin',
            password='admin-password-123',
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.alice)

        response = self.client.post('/admin/login/', {
            'username': admin_user.username,
            'password': 'admin-password-123',
            'next': '/admin/',
        })

        self.assertRedirects(response, '/admin/', fetch_redirect_response=False)
        admin_page = self.client.get('/admin/')
        self.assertEqual(admin_page.status_code, 200)
        self.assertEqual(
            str(self.client.session['_auth_user_id']),
            str(admin_user.pk),
        )

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

    def test_gender_options_are_limited_to_male_and_female(self):
        gender_choices = dict(Profile._meta.get_field('gender').choices)
        preferred_gender_choices = dict(
            Profile._meta.get_field('preferred_gender').choices
        )

        self.assertEqual(gender_choices, {'M': 'Male', 'F': 'Female'})
        self.assertEqual(preferred_gender_choices, gender_choices)
        self.assertEqual(Profile._meta.get_field('gender').default, 'M')

        form = ProfileCreationForm()
        self.assertTrue(form.fields['gender'].required)
        self.assertEqual(
            form.fields['gender'].choices,
            [('M', 'Male'), ('F', 'Female')],
        )
        self.assertEqual(
            form.fields['preferred_gender'].choices,
            [('M', 'Male'), ('F', 'Female')],
        )

    def test_gender_is_included_on_discovery_and_public_profiles(self):
        self.client.force_login(self.alice)

        feed = self.client.get(reverse('get_profiles_json')).json()['profiles']
        self.assertEqual(feed[0]['gender'], 'Male')

        public_profile = self.client.get(
            reverse('public_profile', args=[self.bob.pk])
        )
        self.assertContains(public_profile, 'Male')

    def test_admin_profile_mode_sections_only_show_their_own_profiles(self):
        _, hookup_profile = self.make_profile('admin_hookup', mode='HOOKUP')
        _, sex_call_profile = self.make_profile('admin_sex_call', mode='SEX_CALL')
        admin_user = User.objects.create_superuser(
            username='mode-admin',
            email='mode-admin@example.com',
            password='admin-password-123',
        )
        self.client.force_login(admin_user)

        index_response = self.client.get(reverse('admin:index'))
        self.assertEqual(index_response.status_code, 200)
        app_labels = {
            app_entry['app_label']
            for app_entry in index_response.context['app_list']
        }
        mode_app_labels = {
            'dating_profiles',
            'hookup_profiles',
            'sex_call_profiles',
        }
        self.assertTrue(mode_app_labels.issubset(app_labels))
        app_names = {
            app_entry['app_label']: app_entry['name']
            for app_entry in index_response.context['app_list']
        }
        self.assertEqual(app_names['dating'], 'Platform Management')
        self.assertEqual(app_names['dating_profiles'], 'Dating')
        self.assertEqual(app_names['hookup_profiles'], 'Hookup')
        self.assertEqual(app_names['sex_call_profiles'], 'Sex Call')

        for model, expected_profile, excluded_usernames in (
            (DatingProfile, self.alice_profile, {'admin_hookup', 'admin_sex_call'}),
            (HookupProfile, hookup_profile, {'alice', 'admin_sex_call'}),
            (SexCallProfile, sex_call_profile, {'alice', 'admin_hookup'}),
        ):
            with self.subTest(mode=model._meta.verbose_name):
                response = self.client.get(
                    reverse(f'admin:{model._meta.app_label}_{model._meta.model_name}_changelist')
                )

                self.assertEqual(response.status_code, 200)
                usernames = {
                    profile.user.username
                    for profile in response.context['cl'].result_list
                }
                self.assertIn(expected_profile.user.username, usernames)
                self.assertTrue(excluded_usernames.isdisjoint(usernames))
                self.assertIn(
                    'relationship_mode',
                    admin.site._registry[model].get_exclude(
                        response.wsgi_request,
                        expected_profile,
                    ),
                )
                mode_admin = admin.site._registry[model]
                self.assertNotIn('relationship_mode', mode_admin.list_display)
                self.assertNotIn('relationship_mode', mode_admin.list_filter)
                self.assertNotContains(response, 'relationship mode')

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

    def test_test_profiles_are_hidden_from_members_and_staff(self):
        self.bob_profile.is_test_profile = True
        self.bob_profile.is_verified = False
        self.bob.is_active = True
        self.bob.save(update_fields=['is_active'])
        self.bob_profile.save(update_fields=['is_test_profile', 'is_verified'])

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

        response = self.client.get(
            reverse('get_profiles_json'),
            {'test_profiles': 'on', 'preview_test_profiles': 'on'},
        )
        self.assertFalse(any(
            profile['id'] == self.bob.pk
            for profile in response.json()['profiles']
        ))

    def test_qa_accounts_and_flagged_profiles_are_excluded_from_public_feeds(self):
        qa_user, qa_profile = self.make_profile(
            'qa_demo_legacy',
            gender='F',
            preferred_gender='M',
            mode='DATING',
        )
        test_user, test_profile = self.make_profile(
            'test_user_legacy',
            gender='F',
            preferred_gender='M',
            mode='DATING',
        )
        flagged_user, flagged_profile = self.make_profile(
            'flagged-legacy-test',
            gender='F',
            preferred_gender='M',
            mode='DATING',
        )
        qa_profile.is_test_profile = False
        test_profile.is_test_profile = False
        flagged_profile.is_test_profile = True
        for profile in (qa_profile, test_profile, flagged_profile):
            profile.save()

        self.client.force_login(self.alice)
        excluded_user_ids = {qa_user.pk, test_user.pk, flagged_user.pk}
        self.assertFalse(any(
            profile.user_id in excluded_user_ids
            for profile in get_profile_batch(self.alice, limit=50)
        ))
        response = self.client.get(reverse('get_profiles_json'))
        self.assertFalse(any(
            profile['id'] in excluded_user_ids
            for profile in response.json()['profiles']
        ))

        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save(update_fields=['relationship_mode'])
        for user, profile in (
            (qa_user, qa_profile),
            (test_user, test_profile),
            (flagged_user, flagged_profile),
        ):
            profile.relationship_mode = 'SEX_CALL'
            profile.is_host_ready = True
            profile.save(update_fields=['relationship_mode', 'is_host_ready'])

        sex_call_hosts = self.client.get(reverse('sex_call_hosts_api')).json()['hosts']
        online_hosts = self.client.get(reverse('online_hosts_api')).json()['hosts']
        self.assertFalse(any(
            host['id'] in excluded_user_ids for host in sex_call_hosts
        ))
        self.assertFalse(any(
            host['user_id'] in excluded_user_ids for host in online_hosts
        ))

        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.save(update_fields=['relationship_mode'])
        hookup_response = self.client.get(reverse('hookup_discovery'))
        for username in ('qa_demo_legacy', 'test_user_legacy', 'flagged-legacy-test'):
            self.assertNotContains(hookup_response, username)

        match_response = self.client.get(reverse('match_list'))
        self.assertNotContains(match_response, 'QA profiles for testing')
        self.assertNotContains(match_response, 'qa_demo_legacy')

    def test_qa_cleanup_migration_removes_users_relations_and_media(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                flagged_user, flagged_profile = self.make_profile(
                    'legacy-flagged-profile'
                )
                flagged_profile.is_test_profile = True
                flagged_profile.save(update_fields=['is_test_profile'])
                tag, _ = Tag.objects.get_or_create(name='Cleanup migration tag')
                flagged_profile.tags.add(tag)

                qa_user, _ = self.make_profile(
                    'qa_demo_cleanup',
                    gender='F',
                    preferred_gender='M',
                )
                test_user, _ = self.make_profile(
                    'test_user_cleanup',
                    gender='F',
                    preferred_gender='M',
                )
                orphan_user = User.objects.create_user(
                    username='test_user_orphan',
                    password='unused-password',
                )
                photo = ProfilePhoto(profile=flagged_profile, user=flagged_user)
                photo.image.save(
                    'cleanup-profile.svg',
                    ContentFile(b'<svg/>'),
                    save=True,
                )
                image_name = photo.image.name
                Match.objects.create(
                    user1=self.alice,
                    user2=flagged_user,
                    expires_at=timezone.now() + timedelta(days=1),
                )

                migration = import_module(
                    'dating.migrations.0037_remove_qa_test_profiles'
                )
                migration.remove_qa_test_profiles(
                    apps,
                    SimpleNamespace(connection=connection),
                )

                self.assertFalse(User.objects.filter(
                    pk__in=[
                        flagged_user.pk,
                        qa_user.pk,
                        test_user.pk,
                        orphan_user.pk,
                    ]
                ).exists())
                self.assertFalse(Match.objects.filter(user2=flagged_user).exists())
                self.assertFalse(ProfilePhoto.objects.filter(pk=photo.pk).exists())
                self.assertFalse(photo.image.storage.exists(image_name))
                self.assertTrue(Tag.objects.filter(pk=tag.pk).exists())

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

        self.assertEqual(
            [p.user_id for p in get_profile_batch(self.alice)],
            [self.bob.pk],
        )
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
        settings_form = SettingsForm(instance=self.alice_profile)
        self.assertEqual(set(settings_form.fields), {'is_dnd'})

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
                self.assertTrue(all(profile.is_test_profile for profile in test_profiles))
                self.assertTrue(
                    all(profile.photos.first().image.name.endswith('.svg') for profile in test_profiles)
                )
                self.assertTrue(all(profile.tags.count() == 5 for profile in test_profiles))

    def test_generated_test_profiles_are_never_discoverable(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                call_command('create_test_data', count=27, stdout=StringIO())
                same_gender_user, _ = self.make_profile(
                    'same-gender-seeker',
                    gender='M',
                    preferred_gender='M',
                )
                generated_user_ids = set(
                    Profile.objects.filter(
                        is_test_profile=True,
                        user__username__startswith='test_profile_',
                    ).values_list('user_id', flat=True)
                )
                self.assertFalse(any(
                    profile.user_id in generated_user_ids
                    for profile in get_profile_batch(same_gender_user, limit=1000)
                ))
                self.client.force_login(same_gender_user)
                response = self.client.get(reverse('get_profiles_json'))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(any(
                    profile['id'] in generated_user_ids
                    for profile in response.json()['profiles']
                ))

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

        # Test editing without gender in POST data (gracefully falls back to existing instance.gender)
        response2 = self.client.post(reverse('edit_profile'), {
            'name': 'Alice Fallback',
            'age': 27,
            'preferred_gender': 'M',
            'location': 'Abuja',
        })
        self.assertRedirects(response2, f"{reverse('edit_profile')}?saved=1")
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.gender, 'F')
        self.assertEqual(self.alice_profile.location, 'Abuja')

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
        new_profile = Profile.objects.get(user__username='newmember')
        self.assertEqual(new_profile.relationship_mode, 'SEX_CALL')
        self.assertEqual(new_profile.free_trial_mode, 'SEX_CALL')
        self.assertTrue(new_profile.has_free_trial_access('SEX_CALL'))
        self.assertTrue(new_profile.has_claimed_welcome)
        self.assertTrue(
            CoinTransaction.objects.filter(
                user=new_profile.user,
                transaction_type='WELCOME_BONUS',
            ).exists()
        )

        self.client.logout()
        # Attempting login without connection mode fails
        response = self.client.post(reverse('login'), {
            'email': 'alice@example.com',
            'password': 'test-password-123',
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

        # Attempting to log in with username instead of email fails
        response = self.client.post(reverse('login'), {
            'username': 'alice',
            'password': 'test-password-123',
            'relationship_mode': 'HOOKUP',
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

        # Attempting to log in with username in email field fails
        response = self.client.post(reverse('login'), {
            'email': 'alice',
            'password': 'test-password-123',
            'relationship_mode': 'HOOKUP',
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

        # Logging in with email succeeds
        self.alice_profile.has_paid_mode_switch_fee = True
        self.alice_profile.save(update_fields=['has_paid_mode_switch_fee'])
        response = self.client.post(reverse('login'), {
            'email': 'alice@example.com',
            'password': 'test-password-123',
            'relationship_mode': 'HOOKUP',
        })
        self.assertRedirects(response, reverse('swipe_card'))
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.relationship_mode, 'HOOKUP')
        self.assertEqual(self.client.session['active_connection_mode'], 'HOOKUP')

    def test_primary_routes_render_for_an_authenticated_member(self):
        self.client.force_login(self.alice)

        for route in ('/', '/match/', '/chats/', '/me/', '/settings/', '/support/'):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(
                    response.status_code,
                    200,
                    f'{route} returned {response.status_code}',
                )

    def test_login_cannot_change_mode_before_one_time_unlock(self):
        response = self.client.post(reverse('login'), {
            'email': 'alice@example.com',
            'password': 'test-password-123',
            'relationship_mode': 'HOOKUP',
        })
        self.assertRedirects(
            response,
            f"{reverse('settings')}?mode_switch_required=1&target_mode=HOOKUP",
        )
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.relationship_mode, 'DATING')
        settings_response = self.client.get(response.url)
        self.assertContains(settings_response, 'Your current mode was kept.')
        self.assertContains(settings_response, 'Pay a one-time fee')
        self.assertNotContains(settings_response, '<select id="mode-switch-target"')
        self.assertContains(settings_response, 'data-requested-mode="HOOKUP"')

    def test_referral_rewards_are_idempotent_and_award_hookup_milestones(self):
        config = SiteConfiguration.get_solo()
        config.referral_reward_coins = 100
        config.referrals_per_free_hookup = 2
        config.save()
        starting_balance = self.alice_profile.coin_balance

        referred_users = [
            User.objects.create_user(
                username=f'referred{index}',
                email=f'referred{index}@example.com',
                password='test-password-123',
            )
            for index in (1, 2)
        ]
        for referred_user in referred_users:
            _award_referral_signup_rewards(self.alice.pk, referred_user)

        _award_referral_signup_rewards(self.alice.pk, referred_users[0])

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, starting_balance + 200)
        self.assertEqual(self.alice_profile.hookup_free_connections, 1)
        self.assertEqual(Referral.objects.filter(referrer=self.alice).count(), 2)
        self.assertEqual(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='REFERRAL_REWARD',
            ).count(),
            2,
        )

    def test_signup_referral_link_awards_referrer_and_starts_selected_mode_trial(self):
        referral_url = reverse('signup') + f'?ref={self.alice.username}'
        self.client.get(referral_url)
        response = self.client.post(reverse('signup'), {
            'username': 'referredmember',
            'email': 'referredmember@example.com',
            'first_name': 'Referred',
            'password1': 'secure-password-123',
            'password2': 'secure-password-123',
            'relationship_mode': 'HOOKUP',
            'ref': self.alice.username,
        })
        self.assertRedirects(response, reverse('create_profile'))
        referred_user = User.objects.get(username='referredmember')
        trial_profile = Profile.objects.get(user=referred_user)
        self.assertEqual(trial_profile.relationship_mode, 'HOOKUP')
        self.assertEqual(trial_profile.free_trial_mode, 'HOOKUP')
        self.assertTrue(trial_profile.has_free_trial_access('HOOKUP'))
        self.assertTrue(trial_profile.has_claimed_welcome)
        trial_expiry = trial_profile.free_trial_expires_at
        self.assertTrue(Referral.objects.filter(
            referrer=self.alice,
            referred_user=referred_user,
        ).exists())
        self.alice_profile.refresh_from_db()
        self.assertEqual(
            self.alice_profile.coin_balance,
            self.alice_profile._meta.get_field('coin_balance').get_default() + 100,
        )

        response = self.client.post(reverse('create_profile'), {
            'age': 25,
            'gender': 'F',
            'preferred_gender': 'M',
            'relationship_mode': 'HOOKUP',
        })
        self.assertRedirects(response, reverse('swipe_card'))
        trial_profile.refresh_from_db()
        self.assertEqual(trial_profile.free_trial_mode, 'HOOKUP')
        self.assertTrue(trial_profile.has_free_trial_access('HOOKUP'))
        self.assertFalse(trial_profile.has_free_trial_access('DATING'))
        self.assertEqual(trial_profile.free_trial_expires_at, trial_expiry)

    def test_free_trial_access_expires_and_is_mode_specific(self):
        self.alice_profile.free_trial_mode = 'HOOKUP'
        self.alice_profile.free_trial_expires_at = timezone.now() + timedelta(hours=1)
        self.alice_profile.save(update_fields=[
            'free_trial_mode',
            'free_trial_expires_at',
        ])
        self.assertTrue(self.alice_profile.is_premium('HOOKUP'))
        self.assertFalse(self.alice_profile.is_premium('DATING'))

        self.alice_profile.free_trial_expires_at = timezone.now() - timedelta(seconds=1)
        self.alice_profile.save(update_fields=['free_trial_expires_at'])
        self.assertFalse(self.alice_profile.is_premium('HOOKUP'))

    def test_support_bot_answers_common_questions_without_external_ai(self):
        ticket = SupportTicket.objects.create(user=self.alice)
        with override_settings(GEMINI_API_KEY=''):
            greeting = process_support_inquiry(ticket, 'Hello, are you there?')
            capabilities = process_support_inquiry(ticket, 'What can you do for me?')
            referral = process_support_inquiry(ticket, 'How does the referral reward work?')
            mode_switch = process_support_inquiry(ticket, 'How do I switch my mode?')
            call_monitor = process_support_inquiry(
                ticket,
                'Are live calls monitored for exchanging contact details?',
            )

        self.assertIn('What can I help you with?', greeting)
        self.assertIn('changing modes', capabilities)
        self.assertIn('100 coins', referral)
        self.assertIn('10 successful referrals', referral)
        self.assertIn('paid once', mode_switch)
        self.assertIn('lifetime switching', mode_switch)
        self.assertIn('speech recognition', call_monitor)
        self.assertIn('pending admin review', call_monitor)

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
        self.assertContains(profile_page, 'card-glass')
        self.assertContains(profile_page, 'Edit Profile & Photos')
        self.assertContains(profile_page, 'Online')
        self.assertContains(profile_page, 'Daily Check-In')
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

    def test_support_page_uses_in_app_chat_without_external_contact_links(self):
        response = self.client.get(reverse('contact'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sign in to contact support')
        self.assertContains(response, 'Having trouble with your account or payment? Our support team in Lagos is ready to assist.')
        self.assertNotContains(response, 'wa.me')
        self.assertNotContains(response, 'mailto:')
        self.assertNotContains(response, 'WhatsApp')

        newcomer = User.objects.create_user(
            username='newcomer',
            email='newcomer@example.com',
            password='test-password-123',
        )
        self.client.force_login(newcomer)
        response = self.client.get(reverse('contact'))
        self.assertContains(response, 'Open in-app support chat')
        self.assertContains(response, 'support-chat-window')
        self.assertNotContains(response, 'wa.me')
        self.assertNotContains(response, 'WhatsApp')
        response = self.client.get(reverse('create_profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Connection Style')
        self.assertContains(response, 'Sex Call')

    def test_support_apis_return_json_for_unauthenticated_visitors(self):
        homepage = self.client.get(reverse('index'))
        self.assertEqual(homepage.status_code, 200)
        anonymous_client = Client(enforce_csrf_checks=True)

        endpoints = (
            ('support_messages_api', 'get'),
            ('support_mark_read_api', 'post'),
            ('support_send_api', 'post'),
        )

        for endpoint, method in endpoints:
            with self.subTest(endpoint=endpoint):
                url = reverse(endpoint)
                response = (
                    anonymous_client.get(url)
                    if method == 'get'
                    else anonymous_client.post(
                        url,
                        json.dumps({'message': 'I need help'}),
                        content_type='application/json',
                    )
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers['Content-Type'].startswith('application/json'))
                self.assertEqual(
                    response.json(),
                    {'status': 'unauthenticated', 'messages': []},
                )

    def test_jazzmin_uses_supported_default_theme_setting(self):
        self.assertEqual(settings.JAZZMIN_UI_TWEAKS['default_theme_mode'], 'auto')
        self.assertNotIn('dark_mode_theme', settings.JAZZMIN_UI_TWEAKS)

    def test_non_strict_manifest_storage_falls_back_for_missing_static_reference(self):
        from django.contrib.staticfiles.storage import staticfiles_storage

        with tempfile.TemporaryDirectory() as static_root:
            asset_path = os.path.join(
                static_root,
                'vendor',
                'bootswatch',
                'default',
                'bootstrap.min.css',
            )
            os.makedirs(os.path.dirname(asset_path))
            with open(asset_path, 'wb') as asset_file:
                asset_file.write(b'/* test theme asset */')

            with override_settings(
                STATIC_ROOT=static_root,
                STORAGES={
                    'default': {
                        'BACKEND': 'django.core.files.storage.FileSystemStorage'
                    },
                    'staticfiles': {
                        'BACKEND': (
                            'loveny_site.storage.'
                            'NonStrictManifestStaticFilesStorage'
                        ),
                    },
                },
            ):
                static_url = staticfiles_storage.url(
                    'vendor/bootswatch/default/bootstrap.min.css'
                )
                self.assertTrue(
                    static_url.startswith(
                        '/static/vendor/bootswatch/default/bootstrap.min.'
                    )
                )

    def test_support_apis_continue_to_work_for_authenticated_users(self):
        self.client.force_login(self.alice)

        messages_response = self.client.get(reverse('support_messages_api'))
        self.assertEqual(messages_response.status_code, 200)
        self.assertEqual(messages_response.json()['status'], 'success')

        read_response = self.client.post(reverse('support_mark_read_api'))
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.json()['status'], 'success')

        invalid_message = self.client.post(
            reverse('support_send_api'),
            data='{',
            content_type='application/json',
        )
        self.assertEqual(invalid_message.status_code, 400)
        self.assertEqual(invalid_message.json()['status'], 'error')

    def test_support_greeting_does_not_claim_a_complaint_is_under_review(self):
        self.client.force_login(self.alice)
        with (
            patch('dating.services.support_bot._generate_answer') as generate_answer,
            patch('dating.support_views.threading.Thread.start') as start_thread,
            override_settings(GEMINI_API_KEY=''),
        ):
            first_response = self.post_json(
                reverse('support_send_api'),
                {'message': 'hello', 'current_page': '/app/'},
            )
            self.assertEqual(first_response.status_code, 200)
            self.assertEqual(len(first_response.json()['messages']), 2)
            auto_reply = first_response.json()['messages'][1]['message']
            self.assertIn('automated support assistant', auto_reply)
            self.assertNotIn('reviewing', auto_reply.lower())
            self.assertNotIn('complaint', auto_reply.lower())

            next_response = self.post_json(
                reverse('support_send_api'),
                {'message': 'ok', 'current_page': '/app/'},
            )
            self.assertEqual(next_response.status_code, 200)
            self.assertEqual(len(next_response.json()['messages']), 1)
            start_thread.assert_not_called()
            generate_answer.assert_not_called()

        ticket = SupportTicket.objects.get(user=self.alice)
        self.assertEqual(ticket.status, 'open')
        self.assertEqual(ticket.messages.filter(is_auto_reply=True).count(), 1)

    def test_create_profile_submission_defaults_preferences_and_mode(self):
        new_user = User.objects.create_user(
            username='user27',
            email='user27@example.com',
            password='test-password-123',
        )
        self.client.force_login(new_user)
        # Post form payload without min_age_pref, max_age_pref, max_distance_km
        post_data = {
            'age': 27,
            'gender': 'M',
            'preferred_gender': 'F',
            'relationship_mode': 'SEX_CALL',
            'bio': 'dce',
        }
        response = self.client.post(reverse('create_profile'), post_data)
        self.assertRedirects(response, reverse('sex_call_hub'))
        profile = Profile.objects.get(user=new_user)
        self.assertEqual(profile.age, 27)
        self.assertEqual(profile.gender, 'M')
        self.assertEqual(profile.preferred_gender, 'F')
        self.assertEqual(profile.relationship_mode, 'SEX_CALL')
        self.assertEqual(profile.min_age_pref, 18)
        self.assertEqual(profile.max_age_pref, 50)
        self.assertEqual(profile.max_distance_km, 100)

    def test_incomplete_profile_cannot_bypass_mode_switch_unlock(self):
        new_user = User.objects.create_user(
            username='incompleteprofile',
            email='incompleteprofile@example.com',
            password='test-password-123',
        )
        profile = Profile.objects.create(
            user=new_user,
            relationship_mode='DATING',
            age=None,
        )
        self.client.force_login(new_user)

        response = self.client.post(reverse('create_profile'), {
            'age': 27,
            'gender': 'M',
            'preferred_gender': 'F',
            'relationship_mode': 'HOOKUP',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pay the one-time mode-switch fee from Settings')
        profile.refresh_from_db()
        self.assertEqual(profile.relationship_mode, 'DATING')

    def test_likes_are_hidden_until_premium(self):
        Swipe.objects.create(swiper=self.bob, swiped=self.alice, type='LIKE')
        self.client.force_login(self.alice)
        response = self.client.get(reverse('likes_list'))
        self.assertRedirects(response, reverse('messages_inbox'))
        inbox_response = self.client.get(reverse('messages_inbox'))
        self.assertEqual(inbox_response.status_code, 200)
        self.assertContains(inbox_response, 'MESSAGES')

    def test_empty_messages_actions_follow_active_connection_mode(self):
        self.client.force_login(self.alice)
        mode_destinations = {
            'DATING': (
                'Explore Matches',
                'Explore profiles to start connecting.',
                'swipe_card',
            ),
            'HOOKUP': (
                'Explore Hookups',
                'Explore nearby hookups to start connecting.',
                'hookup_discovery',
            ),
            'SEX_CALL': (
                'Explore Hosts',
                'Explore hosts to start connecting.',
                'sex_call_hub',
            ),
        }

        for mode, (button_label, subtext, route_name) in mode_destinations.items():
            with self.subTest(mode=mode):
                self.alice_profile.relationship_mode = mode
                self.alice_profile.save(update_fields=['relationship_mode'])
                session = self.client.session
                session.pop('active_connection_mode', None)
                session.save()

                response = self.client.get(reverse('messages_inbox'))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['active_connection_mode'], mode)
                self.assertContains(response, button_label)
                self.assertContains(response, subtext)
                self.assertContains(
                    response,
                    f'href="{reverse(route_name)}"',
                )
                self.assertContains(
                    response,
                    f'<a href="{reverse(route_name)}" class="bottom-tab',
                    html=False,
                )

    def test_empty_messages_uses_profile_mode_for_bottom_navigation(self):
        self.client.force_login(self.alice)
        session = self.client.session
        session['active_connection_mode'] = 'HOOKUP'
        session.save()

        response = self.client.get(reverse('messages_inbox'))

        self.assertEqual(response.context['active_connection_mode'], 'DATING')
        self.assertContains(response, 'Explore Matches')
        self.assertContains(
            response,
            f'<a href="{reverse("swipe_card")}" class="bottom-tab',
            html=False,
        )

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
            'type': 'call_offer',
            'payload': {'type': 'offer', 'sdp': 'fake-offer'},
        })
        self.assertEqual(response.status_code, 201)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(signals_url).json()['signals'][0]['type'], 'offer')
        response = self.post_json(send_signal_url, {
            'type': 'call_answer',
            'payload': {'type': 'answer', 'sdp': 'fake-answer'},
        })
        self.assertEqual(response.status_code, 201)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(signals_url).json()['signals'][0]['type'], 'answer')
        candidate = self.post_json(send_signal_url, {
            'type': 'ice_candidate',
            'payload': {'candidate': 'candidate:1 1 UDP 1 127.0.0.1 9 typ host'},
        })
        self.assertEqual(candidate.status_code, 201)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(signals_url).json()['signals'][-1]['type'], 'candidate')
        self.client.force_login(self.alice)
        response = self.post_json(send_signal_url, {
            'type': 'end_call',
            'payload': {'reason': 'test'},
        })
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.assertIsNotNone(call.ended_at)
        self.assertEqual(
            self.post_json(reverse('call_end_api', args=[room_id]), {}).status_code,
            200,
        )

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

    @override_settings(SEX_CALL_BALANCE_GATING_ENABLED=False)
    def test_qa_calls_can_start_and_remain_active_without_coins(self):
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 0
        self.alice_profile.save(update_fields=['relationship_mode', 'coin_balance'])
        self.bob_profile.relationship_mode = 'SEX_CALL'
        self.bob_profile.coin_balance = 0
        self.bob_profile.save(update_fields=['relationship_mode', 'coin_balance'])
        Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            mode='SEX_CALL',
            expires_at=timezone.now() + timedelta(days=1),
        )

        self.client.force_login(self.alice)
        initiated = self.post_json(
            reverse('call_initiate_api'),
            {'receiver_id': self.bob.pk},
        )
        self.assertEqual(initiated.status_code, 201)
        self.assertFalse(initiated.json()['call']['balance_gating_enabled'])
        room_id = initiated.json()['room_id']

        self.client.force_login(self.bob)
        accepted = self.post_json(
            reverse('call_respond_api', args=[room_id]),
            {'action': 'accept'},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()['call']['status'], 'connected')
        self.assertEqual(accepted.json()['call']['coins_spent'], 0)

        call = CallSession.objects.get(room_id=room_id)
        call.started_at = timezone.now() - timedelta(minutes=5)
        call.save(update_fields=['started_at'])
        self.client.force_login(self.alice)
        heartbeat = self.post_json(
            reverse('call_heartbeat_api', args=[room_id]),
            {'duration_seconds': 300},
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.json()['call']['status'], 'connected')
        self.assertEqual(heartbeat.json()['call']['remaining_seconds'], 86400)

        ended = self.post_json(reverse('call_end_api', args=[room_id]), {})
        self.assertEqual(ended.status_code, 200)
        self.alice_profile.refresh_from_db()
        self.bob_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 0)
        self.assertEqual(self.bob_profile.earned_diamonds, 0)
        self.assertFalse(CoinTransaction.objects.filter(call=call).exists())

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

        # 1. Initially newcomer has 0 coins and has not claimed welcome bonus
        response = self.client.get(reverse('wallet_balance_api'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['coin_balance'], 0)
        self.assertFalse(data['has_claimed_welcome'])

        # 2. Claim welcome bonus via popup API
        claim_res = self.client.post(reverse('claim_welcome_bonus_api'))
        self.assertEqual(claim_res.status_code, 200)
        c_data = claim_res.json()
        self.assertEqual(c_data['status'], 'success')
        self.assertEqual(c_data['coins_awarded'], 30)
        self.assertEqual(c_data['coin_balance'], 30)

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 30)
        self.assertTrue(self.alice_profile.has_claimed_welcome)
        self.assertTrue(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='WELCOME_BONUS',
            ).exists()
        )

        # 3. Subsequent claim attempt is blocked
        dup_claim = self.client.post(reverse('claim_welcome_bonus_api'))
        self.assertEqual(dup_claim.status_code, 400)
        self.assertEqual(dup_claim.json()['status'], 'already_claimed')

        # 4. Wallet balance returns updated claimed status
        response2 = self.client.get(reverse('wallet_balance_api'))
        self.assertEqual(response2.json()['coin_balance'], 30)
        self.assertTrue(response2.json()['has_claimed_welcome'])

    def test_visit_rewards_api_grants_welcome_and_daily_rewards_once(self):
        self.client.force_login(self.alice)

        response = self.client.post(reverse('wallet_visit_rewards_api'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['welcome_awarded'], 30)
        self.assertEqual(response.json()['daily_awarded'], 5)
        self.assertEqual(response.json()['coin_balance'], 35)
        self.assertTrue(response.json()['has_claimed_welcome'])
        self.assertTrue(response.json()['has_checked_in_today'])

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 35)
        self.assertEqual(self.alice_profile.checkin_streak, 1)
        self.assertEqual(self.alice_profile.last_checkin_date, timezone.localdate())
        self.assertEqual(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='WELCOME_BONUS',
            ).count(),
            1,
        )
        self.assertEqual(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='DAILY_CHECKIN',
            ).count(),
            1,
        )

        repeat_response = self.client.post(reverse('wallet_visit_rewards_api'))
        self.assertEqual(repeat_response.status_code, 200)
        self.assertEqual(repeat_response.json()['welcome_awarded'], 0)
        self.assertEqual(repeat_response.json()['daily_awarded'], 0)
        self.assertEqual(repeat_response.json()['coin_balance'], 35)

    def test_7_day_streak_awards_diamonds_grand_prize(self):
        self.alice_profile.coin_balance = 0
        self.alice_profile.earned_diamonds = 0
        self.alice_profile.checkin_streak = 6
        self.alice_profile.last_checkin_date = timezone.localdate() - timedelta(days=1)
        self.alice_profile.save(update_fields=['coin_balance', 'earned_diamonds', 'checkin_streak', 'last_checkin_date'])

        self.client.force_login(self.alice)

        res = self.client.post(reverse('daily_checkin_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['checkin_streak'], 7)
        self.assertTrue(data['is_grand_prize'])
        self.assertEqual(data['coins_awarded'], 10)
        self.assertEqual(data['diamonds_awarded'], 20)
        self.assertEqual(data['coin_balance'], 10)
        self.assertEqual(data['earned_diamonds'], 20)

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 10)
        self.assertEqual(self.alice_profile.earned_diamonds, 20)
        self.assertEqual(self.alice_profile.checkin_streak, 7)

    def test_admin_configurable_7_day_rewards(self):
        cfg = SiteConfiguration.get_solo()
        cfg.daily_checkin_coins = 15
        cfg.day_7_bonus_coins = 50
        cfg.day_7_bonus_diamonds = 100
        cfg.save()

        # Day 1 with custom admin coins
        self.alice_profile.coin_balance = 0
        self.alice_profile.save(update_fields=['coin_balance'])
        self.client.force_login(self.alice)

        res = self.client.post(reverse('daily_checkin_api'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['coins_awarded'], 15)
        self.assertEqual(res.json()['coin_balance'], 15)

        # Day 7 with custom admin coins and diamonds
        self.bob_profile.coin_balance = 0
        self.bob_profile.earned_diamonds = 0
        self.bob_profile.checkin_streak = 6
        self.bob_profile.last_checkin_date = timezone.localdate() - timedelta(days=1)
        self.bob_profile.save(update_fields=['coin_balance', 'earned_diamonds', 'checkin_streak', 'last_checkin_date'])

        self.client.force_login(self.bob)
        res_d7 = self.client.post(reverse('daily_checkin_api'))
        self.assertEqual(res_d7.status_code, 200)
        self.assertEqual(res_d7.json()['coins_awarded'], 50)
        self.assertEqual(res_d7.json()['diamonds_awarded'], 100)
        self.assertEqual(res_d7.json()['coin_balance'], 50)
        self.assertEqual(res_d7.json()['earned_diamonds'], 100)

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
        self.assertEqual(gift_data['caller_coins'], 30)
        # 70% of 50 = 35 diamonds
        self.assertEqual(gift_data['diamond_award'], 35)

        self.alice_profile.refresh_from_db()
        self.bob_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 30)
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
        self.assertEqual(g_data['caller_coins'], 5)
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
        self.alice_profile.coin_balance = 0
        self.alice_profile.save()

        self.bob_profile.relationship_mode = 'SEX_CALL'
        self.bob_profile.coin_balance = 0
        self.bob_profile.is_host_ready = True
        self.bob_profile.save()

        self.client.force_login(self.alice)
        first_search = self.post_json(reverse('quick_call_match_api'), {})
        self.assertEqual(first_search.status_code, 200)
        self.assertEqual(first_search.json()['status'], 'waiting')
        self.assertEqual(SexCallQueue.objects.count(), 1)

        second_client = Client()
        second_client.force_login(self.bob)
        second_search = second_client.post(
            reverse('quick_call_match_api'),
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(second_search.status_code, 200)
        data = second_search.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['call']['status'], 'connected')
        self.assertEqual(data['call']['caller_name'], 'bob')
        self.assertEqual(data['matched_user'], 'alice')
        self.assertEqual(SexCallQueue.objects.count(), 0)

        first_result = self.post_json(reverse('quick_call_match_api'), {})
        self.assertEqual(first_result.json()['room_id'], data['room_id'])
        room_url = reverse('call_room_page', args=[data['room_id']])
        self.assertEqual(self.client.get(room_url).status_code, 200)
        self.assertEqual(second_client.get(room_url).status_code, 200)

        async def verify_websocket_offer():
            from channels.routing import URLRouter

            from .routing import websocket_urlpatterns

            app = URLRouter(websocket_urlpatterns)
            alice_socket = WebsocketCommunicator(app, f'/ws/calls/{data["room_id"]}/')
            bob_socket = WebsocketCommunicator(app, f'/ws/calls/{data["room_id"]}/')
            alice_socket.scope['user'] = self.alice
            bob_socket.scope['user'] = self.bob
            room_uuid = UUID(data['room_id'])
            alice_socket.scope['url_route'] = {'kwargs': {'room_id': room_uuid}}
            bob_socket.scope['url_route'] = {'kwargs': {'room_id': room_uuid}}
            alice_connected, _ = await alice_socket.connect()
            bob_connected, _ = await bob_socket.connect()
            self.assertTrue(alice_connected)
            self.assertTrue(bob_connected)
            try:
                acceptance = await alice_socket.receive_json_from()
                bob_acceptance = await bob_socket.receive_json_from()
                self.assertEqual(acceptance['type'], 'call_accepted')
                self.assertEqual(bob_acceptance['type'], 'call_accepted')
                await bob_socket.send_json_to({
                    'type': 'call_offer',
                    'payload': {'type': 'offer', 'sdp': 'test-offer'},
                })
                offer = await alice_socket.receive_json_from()
                self.assertEqual(offer['type'], 'offer')
                self.assertEqual(offer['payload']['sdp'], 'test-offer')
                await alice_socket.send_json_to({
                    'type': 'call_answer',
                    'payload': {'type': 'answer', 'sdp': 'test-answer'},
                })
                answer = await bob_socket.receive_json_from()
                self.assertEqual(answer['type'], 'answer')
                self.assertEqual(answer['payload']['sdp'], 'test-answer')
                await bob_socket.send_json_to({
                    'type': 'end_call',
                    'payload': {'reason': 'test'},
                })
                end_event = await alice_socket.receive_json_from()
                self.assertEqual(end_event['type'], 'end_call')
            finally:
                await alice_socket.disconnect()
                await bob_socket.disconnect()

        async_to_sync(verify_websocket_offer)()

    def test_sex_call_hosts_api_lists_all_online_sex_call_profiles(self):
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save(update_fields=['relationship_mode'])
        self.bob_profile.relationship_mode = 'SEX_CALL'
        self.bob_profile.save(update_fields=['relationship_mode'])
        third_user, third_profile = self.make_profile(
            'third-sex-call-host',
            mode='SEX_CALL',
            gender='F',
            preferred_gender='M',
        )
        dating_user, _ = self.make_profile(
            'dating-only-profile',
            mode='DATING',
            gender='F',
            preferred_gender='M',
        )
        offline_user, offline_profile = self.make_profile(
            'offline-sex-call-host',
            mode='SEX_CALL',
            gender='F',
            preferred_gender='M',
        )
        offline_profile.last_active = timezone.now() - timedelta(hours=1)
        offline_profile.save(update_fields=['last_active'])

        self.client.force_login(self.alice)
        response = self.client.get(reverse('sex_call_hosts_api'))
        self.assertEqual(response.status_code, 200)
        host_ids = {host['id'] for host in response.json()['hosts']}
        self.assertEqual(host_ids, {self.bob.pk, third_user.pk})
        self.assertNotIn(self.alice.pk, host_ids)
        self.assertNotIn(dating_user.pk, host_ids)
        self.assertNotIn(offline_user.pk, host_ids)

    def test_quick_call_matching_relaxes_gender_preference_after_five_seconds(self):
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.preferred_gender = 'F'
        self.alice_profile.save(update_fields=['relationship_mode', 'preferred_gender'])
        self.bob_profile.relationship_mode = 'SEX_CALL'
        self.bob_profile.preferred_gender = 'M'
        self.bob_profile.save(update_fields=['relationship_mode', 'preferred_gender'])
        self.client.force_login(self.alice)
        second_client = Client()
        second_client.force_login(self.bob)

        first_search = self.post_json(reverse('quick_call_match_api'), {})
        self.assertEqual(first_search.json()['status'], 'waiting')
        second_search = second_client.post(
            reverse('quick_call_match_api'),
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(second_search.json()['status'], 'waiting')

        queued_search = SexCallQueue.objects.get(user=self.alice)
        queued_search.created_at = timezone.now() - timedelta(seconds=6)
        queued_search.save(update_fields=['created_at'])
        flexible_match = second_client.post(
            reverse('quick_call_match_api'),
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(flexible_match.status_code, 200)
        self.assertEqual(flexible_match.json()['status'], 'success')
        self.assertEqual(flexible_match.json()['matched_user'], 'alice')

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

    def test_sex_call_hub_cannot_bypass_mode_switch_unlock(self):
        self.client.force_login(self.alice)

        response = self.client.get(reverse('sex_call_hub'))

        self.assertRedirects(
            response,
            f"{reverse('settings')}?mode_switch_required=1&target_mode=SEX_CALL",
        )
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.relationship_mode, 'DATING')

    def test_call_room_page_access_and_controls(self):
        call = CallSession.objects.create(
            caller=self.alice,
            receiver=self.bob,
            status='accepted',
        )

        # Accepted calls stay gated until the call is connected.
        self.client.force_login(self.alice)
        res_alice = self.client.get(reverse('call_room_page', args=[call.room_id]))
        self.assertEqual(res_alice.status_code, 403)
        call.status = 'connected'
        call.save(update_fields=['status'])
        res_alice = self.client.get(reverse('call_room_page', args=[call.room_id]))
        self.assertEqual(res_alice.status_code, 200)
        self.assertContains(res_alice, 'floating-chat-container')
        self.assertContains(res_alice, 'local-video')
        self.assertContains(res_alice, 'remote-video')
        self.assertContains(res_alice, 'call-feedback')
        self.assertContains(res_alice, 'Speech monitoring is active')

        # Bob (receiver) can access the connected call room.
        self.client.force_login(self.bob)
        res_bob = self.client.get(reverse('call_room_page', args=[call.room_id]))
        self.assertEqual(res_bob.status_code, 200)

        # Charlie (third party) is forbidden
        charlie_user, charlie_profile = self.make_profile('charlie', mode='SEX_CALL')
        self.client.force_login(charlie_user)
        res_charlie = self.client.get(reverse('call_room_page', args=[call.room_id]))
        self.assertEqual(res_charlie.status_code, 403)

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

    def test_sex_call_mode_is_only_grid_and_redirects_from_swipe(self):
        self.client.force_login(self.alice)
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.save()

        # Swiping is disabled for Sex Call mode: visiting swipe_card redirects to sex_call_hub
        res_swipe = self.client.get(reverse('swipe_card'))
        self.assertRedirects(res_swipe, reverse('sex_call_hub'))

        # Sex call hub renders ONLY the Grid view with Hot and Nearby filters
        res = self.client.get(reverse('sex_call_hub'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'grid-view-section')
        self.assertContains(res, 'Hot')
        self.assertContains(res, 'Nearby')
        self.assertContains(res, 'hosts-grid')
        self.assertNotContains(res, 'view-mode-swipe')
        self.assertNotContains(res, 'swipe-view-section')

    def test_direct_calling_without_match_in_sex_call_mode_and_dnd_check(self):
        host_user, host_profile = self.make_profile('host_linda', mode='SEX_CALL', age=23)
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 50
        self.alice_profile.save()

        # No match exists between Alice and host_linda
        self.assertFalse(Match.objects.filter(user1=self.alice, user2=host_user).exists())

        # Calling host when DND is enabled returns 403 user_dnd
        host_profile.is_dnd = True
        host_profile.save(update_fields=['is_dnd'])

        self.client.force_login(self.alice)
        res = self.post_json(reverse('call_initiate_api'), {'receiver_id': host_user.pk})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['message'], 'user_dnd')

        # When DND is disabled, call initiates with ringing status and zero upfront deduction
        host_profile.is_dnd = False
        host_profile.save(update_fields=['is_dnd'])

        res = self.post_json(reverse('call_initiate_api'), {'receiver_id': host_user.pk})
        self.assertEqual(res.status_code, 201)
        self.assertIn('room_id', res.json())
        call = CallSession.objects.get(room_id=res.json()['room_id'])
        self.assertEqual(call.status, 'ringing')
        self.assertEqual(call.coins_spent, 0)
        # Alice's coin balance is untouched while dialing
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 50)

    def test_declined_and_cancelled_calls_do_not_deduct_coins(self):
        host_user, host_profile = self.make_profile('host_sarah', mode='SEX_CALL', age=24)
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 50
        self.alice_profile.save()

        # 1. Host declines call
        self.client.force_login(self.alice)
        res = self.post_json(reverse('call_initiate_api'), {'receiver_id': host_user.pk})
        room_id = res.json()['room_id']

        self.client.force_login(host_user)
        dec_res = self.post_json(reverse('call_respond_api', args=[room_id]), {'action': 'decline'})
        self.assertEqual(dec_res.status_code, 200)

        call = CallSession.objects.get(room_id=room_id)
        self.assertEqual(call.status, 'declined')
        self.assertEqual(call.coins_spent, 0)
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 50)

        # 2. Caller cancels call before answer
        self.client.force_login(self.alice)
        res2 = self.post_json(reverse('call_initiate_api'), {'receiver_id': host_user.pk})
        room_id2 = res2.json()['room_id']

        cancel_res = self.post_json(reverse('call_end_api', args=[room_id2]), {})
        self.assertEqual(cancel_res.status_code, 200)

        call2 = CallSession.objects.get(room_id=room_id2)
        self.assertEqual(call2.status, 'cancelled')
        self.assertEqual(call2.coins_spent, 0)
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 50)

    def test_quick_connect_and_say_hi_apis(self):
        target_user, target_profile = self.make_profile('target_john', mode='SEX_CALL', age=25)
        self.client.force_login(self.alice)

        # Quick connect (⚡ Match / Connect)
        conn_res = self.client.post(reverse('quick_connect_api', args=[target_user.pk]))
        self.assertEqual(conn_res.status_code, 200)
        self.assertTrue(conn_res.json()['is_matched'])
        self.assertTrue(Match.objects.filter(
            Q(user1=self.alice, user2=target_user) | Q(user2=self.alice, user1=target_user)
        ).exists())

        # Quick say hi (💬 Say Hi)
        hi_res = self.client.post(reverse('quick_say_hi_api', args=[target_user.pk]))
        self.assertEqual(hi_res.status_code, 200)
        self.assertIn('redirect_url', hi_res.json())
        conv = Conversation.objects.filter(participants=self.alice).filter(participants=target_user).first()
        self.assertIsNotNone(conv)
        self.assertTrue(conv.messages.filter(sender=self.alice).exists())

    def test_public_profile_view_chamet_standards(self):
        target_user, target_profile = self.make_profile('host_bella', mode='SEX_CALL', age=22)
        # Add 3 photos
        p1 = ProfilePhoto.objects.create(profile=target_profile, image='profile_media/staff_test_f.svg', is_main=True)
        p2 = ProfilePhoto.objects.create(profile=target_profile, image='profile_media/staff_test_m.svg', is_main=False)
        p3 = ProfilePhoto.objects.create(profile=target_profile, image='profile_media/staff_test_f.svg', is_main=False)

        self.client.force_login(self.alice)
        res = self.client.get(reverse('public_profile', args=[target_user.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Instant Video Call')
        self.assertContains(res, 'Say Hi')
        self.assertContains(res, 'Match')
        self.assertContains(res, 'Identity Gallery')
        self.assertContains(res, 'Voice Intro')
        self.assertContains(res, 'Gifts Received')
        self.assertContains(res, 'Languages Spoken')
        self.assertContains(res, 'Call Rate')

    def test_daily_checkin_api_and_me_hub_view(self):
        self.alice_profile.coin_balance = 20
        self.alice_profile.save(update_fields=['coin_balance'])
        self.client.force_login(self.alice)

        # 1. Daily checkin awards +5 coins
        checkin_res = self.client.post(reverse('daily_checkin_api'))
        self.assertEqual(checkin_res.status_code, 200)
        c_data = checkin_res.json()
        self.assertEqual(c_data['status'], 'success')
        self.assertEqual(c_data['coins_awarded'], 5)
        self.assertEqual(c_data['coin_balance'], 25)

        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.coin_balance, 25)
        self.assertTrue(
            CoinTransaction.objects.filter(
                user=self.alice,
                transaction_type='DAILY_CHECKIN',
                amount=5,
            ).exists()
        )

        # 2. Second checkin on same day is blocked
        dup_res = self.client.post(reverse('daily_checkin_api'))
        self.assertEqual(dup_res.status_code, 400)
        self.assertEqual(dup_res.json()['status'], 'already_claimed')

        # 3. Access Me Hub (/profile/ and /profile/me/)
        for path_name in ('profile', 'profile_me'):
            res = self.client.get(reverse(path_name))
            self.assertEqual(res.status_code, 200)
            self.assertContains(res, 'My Profile')
            self.assertContains(res, 'ID: 317')
            self.assertContains(res, 'Daily Check-In')
            self.assertContains(res, 'Customer Service')
            self.assertContains(res, 'Recharge')

    def test_chamet_chat_room_view_and_image_sending(self):
        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.client.force_login(self.alice)
        res = self.client.get(reverse('chat_room', args=[match.id]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Icebreakers:')
        self.assertContains(res, 'Hello, how are you?')
        self.assertContains(res, 'Photo')
        self.assertContains(res, 'Video Call')
        self.assertContains(res, 'Real-person Verified')

        # Test sending image message via conversation_send_api
        conv = Conversation.objects.filter(participants=self.alice).filter(participants=self.bob).first()
        self.assertIsNotNone(conv)
        send_res = self.post_json(
            reverse('conversation_send_api', args=[conv.pk]),
            {
                'text': '📷 Photo',
                'message_type': 'image',
                'metadata': {'image_url': 'https://example.com/test.jpg'}
            }
        )
        self.assertEqual(send_res.status_code, 201)
        self.assertEqual(send_res.json()['message']['message_type'], 'image')

    def test_content_filter_detection_and_blocking(self):
        blocked_messages = [
            "Call me on 08012345678",
            "Reach me: +234 801 234 5678",
            "Price ₦08012345678",
            "Number: 0 8 0 3 - 1 2 3 - 4 5 6 7",
            "080.3123.4567",
            "Bank account 1234567890",
            "wh@ts@pp me later",
            "w.h.a.t.s.a.p.p",
            "t.e.l.e.g.r.a.m me",
            "wa.me/2348012345678",
            "@privatehandle",
            "zero eight zero one two three four five six seven",
            "zeero ate won tu tri for five six seven",
            "I, II, III, IV, V, VI, VII",
        ]
        for message in blocked_messages:
            with self.subTest(message=message):
                blocked, _ = ContentFilter.inspect(message)
                self.assertTrue(blocked)

        blocked, _ = ContentFilter.inspect("Hey, it is so nice to meet you on LOVENY!")
        self.assertFalse(blocked)

        # 5. Blocking inside conversation_send_api & incrementing off_platform_attempts
        match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            expires_at=timezone.now() + timedelta(days=1),
        )
        conv = Conversation.objects.create()
        conv.participants.add(self.alice, self.bob)
        self.client.force_login(self.alice)

        initial_attempts = self.alice.profile.off_platform_attempts
        blocked_res = self.post_json(
            reverse('conversation_send_api', args=[conv.pk]),
            {'text': 'Text me on 08012345678 please'}
        )
        self.assertEqual(blocked_res.status_code, 400)
        self.assertEqual(blocked_res.json()['message'], 'Sharing contact details off-platform is strictly prohibited.')

        self.alice.profile.refresh_from_db()
        self.assertEqual(self.alice.profile.off_platform_attempts, initial_attempts + 1)

    def test_settings_page_read_only_mode_and_dnd(self):
        self.client.force_login(self.alice)
        res = self.client.get(reverse('settings'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Active Mode:')
        self.assertContains(res, 'Do Not Disturb (DND)')
        self.assertContains(res, 'Change connection mode')
        self.assertContains(res, 'mode-switch-target')
        self.assertEqual(list(SettingsForm(instance=self.alice.profile).fields), ['is_dnd'])
        # Ensure select dropdown for relationship mode is removed
        self.assertNotContains(res, '<select name="relationship_mode"')

        # Test toggling DND via settings post
        self.alice.profile.relationship_mode = 'HOOKUP'
        self.alice.profile.incognito_mode = True
        self.alice.profile.save(update_fields=['relationship_mode', 'incognito_mode'])
        post_res = self.client.post(reverse('settings'), {
            'is_dnd': 'on',
        }, follow=True)
        self.assertEqual(post_res.status_code, 200)
        self.alice.profile.refresh_from_db()
        self.assertTrue(self.alice.profile.is_dnd)
        self.assertEqual(self.alice.profile.relationship_mode, 'HOOKUP')
        self.assertTrue(self.alice.profile.incognito_mode)

    def test_mode_switch_requires_a_bound_verified_payment_and_preserves_profile_backup(self):
        self.client.force_login(self.alice)
        self.alice_profile.bio = 'My Dating bio'
        self.alice_profile.first_date_idea = 'Coffee by the beach'
        self.alice_profile.job_title = 'Designer'
        self.alice_profile.save(update_fields=['bio', 'first_date_idea', 'job_title'])
        configuration = SiteConfiguration.get_solo()
        configuration.mode_switch_fee = 250
        configuration.save(update_fields=['mode_switch_fee'])

        with override_settings(
            PAYSTACK_PUBLIC_KEY='pk_test_mode_switch',
            PAYSTACK_SECRET_KEY='sk_test_mode_switch',
        ):
            intent_response = self.post_json(
                reverse('create_mode_switch_payment_intent_api'),
                {'target_mode': 'HOOKUP'},
            )
            self.assertEqual(intent_response.status_code, 200)
            intent_data = intent_response.json()
            self.assertEqual(intent_data['amount_kobo'], 25000)
            intent = ModeSwitchPaymentIntent.objects.get(reference=intent_data['reference'])

            missing_reference = self.post_json(
                reverse('switch_profile_mode_api'),
                {},
            )
            self.assertEqual(missing_reference.status_code, 400)

            with patch('dating.views._verify_paystack_reference', return_value=({
                'status': True,
                'data': {
                    'status': 'success',
                    'reference': intent.reference,
                    'amount': 24999,
                    'currency': 'NGN',
                },
            }, None, None)):
                underpaid = self.post_json(
                    reverse('switch_profile_mode_api'),
                    {'reference': intent.reference},
                )
            self.assertEqual(underpaid.status_code, 400)
            self.alice_profile.refresh_from_db()
            self.assertEqual(self.alice_profile.relationship_mode, 'DATING')

            with patch('dating.views._verify_paystack_reference', return_value=({
                'status': True,
                'data': {
                    'status': 'success',
                    'reference': intent.reference,
                    'amount': 25000,
                    'currency': 'NGN',
                },
            }, None, None)):
                verified = self.post_json(
                    reverse('switch_profile_mode_api'),
                    {'reference': intent.reference},
                )
            self.assertEqual(verified.status_code, 200)
            self.alice_profile.refresh_from_db()
            self.assertEqual(self.alice_profile.relationship_mode, 'HOOKUP')
            backup = self.alice.profile_backups.get(mode='DATING')
            self.assertEqual(backup.bio, 'My Dating bio')
            self.assertEqual(backup.first_date_idea, 'Coffee by the beach')
            self.assertEqual(backup.job_title, 'Designer')
            self.assertTrue(PaymentTransaction.objects.filter(
                reference=intent.reference,
                product='MODE_SWITCH',
                amount_kobo=25000,
            ).exists())
            intent.refresh_from_db()
            self.assertIsNotNone(intent.completed_at)
            self.alice_profile.refresh_from_db()
            self.assertTrue(self.alice_profile.has_paid_mode_switch_fee)

            replay = self.post_json(
                reverse('switch_profile_mode_api'),
                {'reference': intent.reference},
            )
            self.assertEqual(replay.status_code, 400)

            switch_back = self.post_json(
                reverse('switch_profile_mode_api'),
                {'target_mode': 'DATING'},
            )
            self.assertEqual(switch_back.status_code, 200)
            self.alice_profile.refresh_from_db()
            self.assertEqual(self.alice_profile.relationship_mode, 'DATING')

            unlocked_intent = self.post_json(
                reverse('create_mode_switch_payment_intent_api'),
                {'target_mode': 'SEX_CALL'},
            )
            self.assertEqual(unlocked_intent.status_code, 200)
            self.assertTrue(unlocked_intent.json()['switching_unlocked'])
            self.assertNotIn('amount_kobo', unlocked_intent.json())

            switch_again = self.post_json(
                reverse('switch_profile_mode_api'),
                {'target_mode': 'SEX_CALL'},
            )
            self.assertEqual(switch_again.status_code, 200)
            self.alice_profile.refresh_from_db()
            self.assertEqual(self.alice_profile.relationship_mode, 'SEX_CALL')
            self.assertEqual(
                PaymentTransaction.objects.filter(
                    user=self.alice,
                    product='MODE_SWITCH',
                ).count(),
                1,
            )

    def test_mode_switch_checkout_rejects_mismatched_paystack_key_environments(self):
        self.client.force_login(self.alice)
        with override_settings(
            PAYSTACK_PUBLIC_KEY='pk_live_public_test',
            PAYSTACK_SECRET_KEY='sk_test_secret_test',
        ):
            response = self.post_json(
                reverse('create_mode_switch_payment_intent_api'),
                {'target_mode': 'HOOKUP'},
            )
            settings_response = self.client.get(reverse('settings'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['message'], 'payments_unavailable')
        self.assertEqual(ModeSwitchPaymentIntent.objects.count(), 0)
        self.assertNotContains(
            settings_response,
            'UNLOCK MODE SWITCHING (₦200)',
        )
        self.assertContains(
            settings_response,
            'temporarily unavailable because Paystack payments are not configured',
        )

    def test_paystack_authorization_errors_explain_configuration_problem(self):
        from urllib.error import HTTPError

        self.client.force_login(self.alice)
        with override_settings(
            PAYSTACK_PUBLIC_KEY='pk_live_public_test',
            PAYSTACK_SECRET_KEY='sk_live_secret_test',
        ):
            intent_response = self.post_json(
                reverse('create_mode_switch_payment_intent_api'),
                {'target_mode': 'HOOKUP'},
            )
            reference = intent_response.json()['reference']
            with patch(
                'dating.views.urlopen',
                side_effect=HTTPError(
                    'https://api.paystack.co/transaction/verify/test',
                    401,
                    'Unauthorized',
                    {},
                    None,
                ),
            ):
                response = self.post_json(
                    reverse('switch_profile_mode_api'),
                    {'reference': reference},
                )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()['message'],
            'Payment verification is misconfigured. Please contact support before retrying.',
        )
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.relationship_mode, 'DATING')
        self.assertFalse(self.alice_profile.has_paid_mode_switch_fee)

    def test_mode_switch_payment_intent_can_be_cancelled_and_cannot_be_used_afterward(self):
        self.client.force_login(self.alice)
        with override_settings(
            PAYSTACK_PUBLIC_KEY='pk_test_mode_switch',
            PAYSTACK_SECRET_KEY='sk_test_mode_switch',
        ):
            intent_response = self.post_json(
                reverse('create_mode_switch_payment_intent_api'),
                {'target_mode': 'SEX_CALL'},
            )
            self.assertEqual(intent_response.status_code, 200)
            reference = intent_response.json()['reference']

            cancel_response = self.post_json(
                reverse('cancel_mode_switch_payment_intent_api'),
                {'reference': reference},
            )
            self.assertEqual(cancel_response.status_code, 200)
            self.assertTrue(cancel_response.json()['cancelled'])

            with patch('dating.views._verify_paystack_reference') as verify_payment:
                verify_response = self.post_json(
                    reverse('switch_profile_mode_api'),
                    {'reference': reference},
                )
            self.assertEqual(verify_response.status_code, 400)
            verify_payment.assert_not_called()

    def test_call_signal_privacy_and_mutual_face_verification(self):
        call = CallSession.objects.create(
            caller=self.alice,
            receiver=self.bob,
            status='connected',
        )
        self.client.force_login(self.alice)

        # Send privacy blur engaged signal
        blur_res = self.post_json(
            reverse('call_signal_send_api', args=[call.room_id]),
            {'type': 'privacy_blur_engaged', 'payload': {'blurred': True}}
        )
        self.assertEqual(blur_res.status_code, 201)

        # Alice sends face_verified signal
        alice_face_res = self.post_json(
            reverse('call_signal_send_api', args=[call.room_id]),
            {'type': 'face_verified', 'payload': {'verified': True}}
        )
        self.assertEqual(alice_face_res.status_code, 201)
        alice_monitor_res = self.post_json(
            reverse('call_signal_send_api', args=[call.room_id]),
            {'type': 'speech_monitor_ready', 'payload': {'ready': True}},
        )
        self.assertEqual(alice_monitor_res.status_code, 201)

        forged_unlock = self.post_json(
            reverse('call_signal_send_api', args=[call.room_id]),
            {'type': 'feeds_unlocked', 'payload': {'unlocked': True}},
        )
        self.assertEqual(forged_unlock.status_code, 400)

        # feeds_unlocked should not yet exist (Bob hasn't verified yet)
        self.assertFalse(call.signals.filter(signal_type='feeds_unlocked').exists())

        # Bob logs in and sends face_verified signal
        self.client.force_login(self.bob)
        bob_face_res = self.post_json(
            reverse('call_signal_send_api', args=[call.room_id]),
            {'type': 'face_verified', 'payload': {'verified': True}}
        )
        self.assertEqual(bob_face_res.status_code, 201)
        self.assertFalse(call.signals.filter(signal_type='feeds_unlocked').exists())
        bob_monitor_res = self.post_json(
            reverse('call_signal_send_api', args=[call.room_id]),
            {'type': 'speech_monitor_ready', 'payload': {'ready': True}},
        )
        self.assertEqual(bob_monitor_res.status_code, 201)

        # Both users need face and live speech checks before feeds unlock.
        self.assertTrue(call.signals.filter(signal_type='feeds_unlocked').exists())

    def test_live_call_contact_exchange_suspends_speaker_and_creates_private_moderation_report(self):
        call = CallSession.objects.create(
            caller=self.alice,
            receiver=self.bob,
            status='connected',
            started_at=timezone.now() - timedelta(seconds=35),
            rate_per_minute=20,
        )
        self.client.force_login(self.alice)

        response = self.post_json(
            reverse('call_moderation_flag_api', args=[call.room_id]),
            {'transcript': 'My WhatsApp number is 0803 123 4567'},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['status'], 'suspended')
        self.alice.refresh_from_db()
        self.alice_profile.refresh_from_db()
        call.refresh_from_db()
        report = UserReport.objects.get(reported=self.alice, reason='off_platform')
        signal = call.signals.get(signal_type='contact_exchange_flagged')
        self.assertFalse(self.alice.is_active)
        self.assertEqual(self.alice_profile.off_platform_attempts, 1)
        self.assertEqual(call.status, 'ended')
        self.assertEqual(report.reporter, self.bob)
        self.assertIn(str(call.room_id), report.details)
        self.assertIn('Transcript text was not retained.', report.details)
        self.assertNotIn('0803 123 4567', report.details)
        self.assertNotIn('WhatsApp', report.details)
        self.assertEqual(signal.sender, self.alice)
        self.assertNotIn('transcript', signal.payload)
        self.assertEqual(
            self.client.session['live_call_suspension_notice'],
            response.json()['message'],
        )

    def test_live_call_monitor_does_not_suspend_for_clean_speech(self):
        call = CallSession.objects.create(
            caller=self.alice,
            receiver=self.bob,
            status='connected',
            started_at=timezone.now(),
        )
        self.client.force_login(self.alice)

        response = self.post_json(
            reverse('call_moderation_flag_api', args=[call.room_id]),
            {'transcript': 'It is nice to meet you on LOVENY.'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'clear')
        self.alice.refresh_from_db()
        call.refresh_from_db()
        self.assertTrue(self.alice.is_active)
        self.assertEqual(call.status, 'connected')
        self.assertFalse(UserReport.objects.filter(reported=self.alice).exists())

    def test_login_shows_live_call_suspension_notice(self):
        session = self.client.session
        session['live_call_suspension_notice'] = (
            'Your account was temporarily suspended pending admin review.'
        )
        session.save()
        response = self.client.get(reverse('login'))

        self.assertContains(response, 'Account temporarily suspended')
        self.assertContains(response, 'pending admin review')
        self.assertNotIn('live_call_suspension_notice', self.client.session)

    def test_sex_call_free_trial_can_start_and_accept_calls_without_coin_balance(self):
        self.alice_profile.relationship_mode = 'SEX_CALL'
        self.alice_profile.coin_balance = 0
        self.alice_profile.free_trial_mode = 'SEX_CALL'
        self.alice_profile.free_trial_expires_at = timezone.now() + timedelta(hours=1)
        self.alice_profile.save(update_fields=[
            'relationship_mode',
            'coin_balance',
            'free_trial_mode',
            'free_trial_expires_at',
        ])
        self.client.force_login(self.alice)

        initiated = self.post_json(
            reverse('call_initiate_api'),
            {'receiver_id': self.bob.pk},
        )
        self.assertEqual(initiated.status_code, 201)
        room_id = initiated.json()['room_id']

        self.client.force_login(self.bob)
        accepted = self.post_json(
            reverse('call_respond_api', args=[room_id]),
            {'action': 'accept'},
        )
        self.assertEqual(accepted.status_code, 200)
        call = CallSession.objects.get(room_id=room_id)
        self.assertEqual(call.status, 'connected')
        self.assertEqual(call.coins_spent, 0)

    def test_hookup_mode_lifecycle_and_paystack_webhook(self):
        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.save(update_fields=['relationship_mode'])
        self.bob_profile.relationship_mode = 'HOOKUP'
        self.bob_profile.save(update_fields=['relationship_mode'])
        self.client.force_login(self.alice)

        # 1. Send hookup request
        req_res = self.post_json(
            reverse('hookup_request_api'),
            {'target_id': self.bob.pk}
        )
        self.assertEqual(req_res.status_code, 201)
        match_id = req_res.json()['match_id']
        match = HookupMatch.objects.get(pk=match_id)
        self.assertEqual(match.status, 'pending')

        # 2. Bob accepts hookup request
        self.client.force_login(self.bob)
        accept_res = self.post_json(
            reverse('hookup_respond_api', args=[match.id]),
            {'action': 'accept'}
        )
        self.assertEqual(accept_res.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, 'accepted')

        # 3. Simulate Alice paying fee via webhook
        with override_settings(PAYSTACK_SECRET_KEY='test_secret'):
            body_alice = json.dumps({
                'event': 'charge.success',
                'data': {
                    'reference': f'HOOKUP_{match.id}_ALICE',
                    'status': 'success',
                    'amount': 150000,
                    'currency': 'NGN',
                    'customer': {'email': self.alice.email},
                    'metadata': {
                        'match_id': str(match.id),
                        'payer_id': self.alice.id,
                        'product': 'HOOKUP_MATCH',
                    }
                }
            }).encode('utf-8')

            import hmac
            import hashlib
            sig_alice = hmac.new(b'test_secret', body_alice, hashlib.sha512).hexdigest()

            wh_res1 = self.client.post(
                reverse('paystack_webhook'),
                data=body_alice,
                content_type='application/json',
                HTTP_X_PAYSTACK_SIGNATURE=sig_alice,
            )
            self.assertEqual(wh_res1.status_code, 200)

            match.refresh_from_db()
            self.assertTrue(match.initiator_paid)
            self.assertFalse(match.is_fully_paid())
            self.assertEqual(match.status, 'accepted')

            # 4. Simulate Bob paying fee via webhook
            body_bob = json.dumps({
                'event': 'charge.success',
                'data': {
                    'reference': f'HOOKUP_{match.id}_BOB',
                    'status': 'success',
                    'amount': 150000,
                    'currency': 'NGN',
                    'customer': {'email': self.bob.email},
                    'metadata': {
                        'match_id': str(match.id),
                        'payer_id': self.bob.id,
                        'product': 'HOOKUP_MATCH',
                    }
                }
            }).encode('utf-8')
            sig_bob = hmac.new(b'test_secret', body_bob, hashlib.sha512).hexdigest()

            wh_res2 = self.client.post(
                reverse('paystack_webhook'),
                data=body_bob,
                content_type='application/json',
                HTTP_X_PAYSTACK_SIGNATURE=sig_bob,
            )
            self.assertEqual(wh_res2.status_code, 200)

            match.refresh_from_db()
            self.assertTrue(match.target_paid)
            self.assertTrue(match.is_fully_paid())
            self.assertEqual(match.status, 'unlocked')
            self.assertIsNotNone(match.unlocked_at)
            self.assertIsNotNone(match.expires_at)

        # 5. Test unlocked hookup chat
        self.client.force_login(self.alice)
        chat_view_res = self.client.get(reverse('hookup_chat_room', args=[match.id]))
        self.assertEqual(chat_view_res.status_code, 200)
        self.assertContains(chat_view_res, 'Chat Window:')

        # 6. Send clean hookup message
        send_msg_res = self.post_json(
            reverse('hookup_send_message_api', args=[match.id]),
            {'text': 'Hello tonight! Where are you located?'}
        )
        self.assertEqual(send_msg_res.status_code, 201)
        self.assertEqual(match.messages.count(), 1)

        # 7. Send contact leak message in hookup chat (should be blocked)
        leak_msg_res = self.post_json(
            reverse('hookup_send_message_api', args=[match.id]),
            {'text': 'My whatsapp is 08012345678'}
        )
        self.assertEqual(leak_msg_res.status_code, 400)
        self.assertEqual(leak_msg_res.json()['message'], 'Sharing contact details off-platform is strictly prohibited.')

    def test_referral_free_hookup_connection_is_consumed_and_restored_on_decline(self):
        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.hookup_free_connections = 1
        self.alice_profile.save(update_fields=[
            'relationship_mode',
            'hookup_free_connections',
        ])
        self.bob_profile.relationship_mode = 'HOOKUP'
        self.bob_profile.save(update_fields=['relationship_mode'])
        self.client.force_login(self.alice)

        first_request = self.post_json(
            reverse('hookup_request_api'),
            {'target_id': self.bob.pk},
        )
        self.assertEqual(first_request.status_code, 201)
        first_match = HookupMatch.objects.get(pk=first_request.json()['match_id'])
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.hookup_free_connections, 0)
        self.assertTrue(first_match.initiator_paid)
        self.assertTrue(first_match.initiator_referral_free)

        self.client.force_login(self.bob)
        decline = self.post_json(
            reverse('hookup_respond_api', args=[first_match.pk]),
            {'action': 'decline'},
        )
        self.assertEqual(decline.status_code, 200)
        self.alice_profile.refresh_from_db()
        first_match.refresh_from_db()
        self.assertEqual(self.alice_profile.hookup_free_connections, 1)
        self.assertFalse(first_match.initiator_paid)
        self.assertFalse(first_match.initiator_referral_free)

        self.client.force_login(self.alice)
        second_request = self.post_json(
            reverse('hookup_request_api'),
            {'target_id': self.bob.pk},
        )
        self.assertEqual(second_request.status_code, 201)
        second_match = HookupMatch.objects.get(pk=second_request.json()['match_id'])
        self.client.force_login(self.bob)
        accept = self.post_json(
            reverse('hookup_respond_api', args=[second_match.pk]),
            {'action': 'accept'},
        )
        self.assertEqual(accept.status_code, 200)
        second_match.refresh_from_db()
        self.assertEqual(second_match.status, 'accepted')
        self.assertTrue(second_match.initiator_paid)
        self.assertTrue(second_match.initiator_referral_free)

    def test_hookup_route_does_not_switch_dating_profile(self):
        self.client.force_login(self.alice)

        response = self.client.get(reverse('hookup_discovery'))

        self.assertRedirects(response, reverse('settings'))
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.relationship_mode, 'DATING')

    def test_dating_recipient_cannot_open_hookup_inbox_or_receive_hookup_invitation(self):
        HookupMatch.objects.create(
            initiator=self.bob,
            target=self.alice,
            status='pending',
        )
        self.client.force_login(self.alice)

        response = self.client.get(reverse('hookup_discovery'))

        self.assertRedirects(response, reverse('settings'))
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.relationship_mode, 'DATING')
        notifications = self.client.get(reverse('global_notifications_api')).json()
        self.assertEqual(notifications['incoming_hookup_requests'], [])

    def test_hookup_request_requires_both_profiles_in_hookup_mode_and_notifies_recipient(self):
        self.client.force_login(self.alice)
        dating_response = self.post_json(
            reverse('hookup_request_api'),
            {'target_id': self.bob.pk},
        )
        self.assertEqual(dating_response.status_code, 403)
        self.assertEqual(HookupMatch.objects.count(), 0)

        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.save(update_fields=['relationship_mode'])
        self.bob_profile.relationship_mode = 'HOOKUP'
        self.bob_profile.save(update_fields=['relationship_mode'])
        request_response = self.post_json(
            reverse('hookup_request_api'),
            {'target_id': self.bob.pk},
        )
        self.assertEqual(request_response.status_code, 201)
        match_id = request_response.json()['match_id']

        duplicate_response = self.post_json(
            reverse('hookup_request_api'),
            {'target_id': self.bob.pk},
        )
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(duplicate_response.json()['status'], 'exists')
        self.assertEqual(duplicate_response.json()['request_direction'], 'outgoing')
        self.assertEqual(HookupMatch.objects.count(), 1)

        self.client.force_login(self.bob)
        reverse_response = self.post_json(
            reverse('hookup_request_api'),
            {'target_id': self.alice.pk},
        )
        self.assertEqual(reverse_response.status_code, 200)
        self.assertEqual(reverse_response.json()['status'], 'exists')
        self.assertEqual(reverse_response.json()['request_direction'], 'incoming')
        self.assertEqual(HookupMatch.objects.count(), 1)

        notifications = self.client.get(reverse('global_notifications_api')).json()
        self.assertEqual(
            notifications['incoming_hookup_requests'],
            [{
                'id': match_id,
                'initiator_name': self.alice.username,
            }],
        )
        inbox = self.client.get(reverse('hookup_discovery'))
        self.assertEqual(inbox.status_code, 200)
        self.assertEqual(
            [str(match.pk) for match in inbox.context['pending_incoming']],
            [match_id],
        )

    def test_notifications_are_mode_scoped_and_acknowledged_server_side(self):
        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.save(update_fields=['relationship_mode'])
        self.bob_profile.relationship_mode = 'HOOKUP'
        self.bob_profile.save(update_fields=['relationship_mode'])
        hookup_request = HookupMatch.objects.create(
            initiator=self.bob,
            target=self.alice,
            status='pending',
        )

        self.client.force_login(self.alice)
        notifications_url = reverse('global_notifications_api')
        response = self.client.get(notifications_url)
        self.assertEqual(
            response.json()['incoming_hookup_requests'],
            [{
                'id': str(hookup_request.pk),
                'initiator_name': self.bob.username,
            }],
        )

        ack_url = reverse('acknowledge_notifications_api')
        ack_response = self.post_json(ack_url, {
            'hookup_request_ids': [str(hookup_request.pk)],
        })
        self.assertEqual(ack_response.status_code, 200)
        hookup_request.refresh_from_db()
        self.assertIsNotNone(hookup_request.target_notification_delivered_at)
        self.assertEqual(
            self.client.get(notifications_url).json()['incoming_hookup_requests'],
            [],
        )

        self.bob_profile.relationship_mode = 'DATING'
        self.bob_profile.save(update_fields=['relationship_mode'])
        undelivered_request = HookupMatch.objects.create(
            initiator=self.bob,
            target=self.alice,
            status='pending',
        )
        self.assertNotIn(
            str(undelivered_request.pk),
            {
                item['id']
                for item in self.client.get(notifications_url).json()['incoming_hookup_requests']
            },
        )

        self.alice_profile.relationship_mode = 'DATING'
        self.alice_profile.save(update_fields=['relationship_mode'])
        self.client.force_login(self.alice)
        dating_match = Match.objects.create(
            user1=self.alice,
            user2=self.bob,
            mode='DATING',
            expires_at=timezone.now() + timedelta(days=1),
        )
        conversation = Conversation.objects.create()
        conversation.participants.add(self.alice, self.bob)
        ChatMessage.objects.create(
            conversation=conversation,
            sender=self.alice,
            text='Earlier message',
        )
        message = ChatMessage.objects.create(
            conversation=conversation,
            sender=self.bob,
            text='Dating message',
        )

        visible = self.client.get(
            notifications_url,
            {'last_id': message.pk - 1},
        ).json()
        self.assertEqual([row['id'] for row in visible['new_messages']], [message.pk])
        message_ack = self.post_json(ack_url, {'message_ids': [message.pk]})
        self.assertEqual(message_ack.status_code, 200)
        self.assertEqual(message_ack.json()['messages_acknowledged'], 1)
        redelivered = self.client.get(
            notifications_url,
            {'last_id': message.pk - 1},
        ).json()
        self.assertEqual(redelivered['new_messages'], [])

        self.alice_profile.relationship_mode = 'HOOKUP'
        self.alice_profile.save(update_fields=['relationship_mode'])
        isolated = self.client.get(
            notifications_url,
            {'last_id': message.pk - 1},
        ).json()
        self.assertEqual(isolated['new_messages'], [])
        self.assertEqual(isolated['unread_count'], 0)

    def test_hookup_payment_api_requires_a_verified_paystack_transaction(self):
        match = HookupMatch.objects.create(
            initiator=self.alice,
            target=self.bob,
            status='accepted',
        )
        self.client.force_login(self.alice)
        with patch('dating.views._verify_paystack_reference', return_value=({
            'status': True,
            'data': {
                'status': 'failed',
                'reference': 'forged-reference',
                'amount': 150000,
                'currency': 'NGN',
                'metadata': {
                    'match_id': str(match.pk),
                    'payer_id': self.alice.pk,
                    'product': 'HOOKUP_MATCH',
                },
            },
        }, None, None)):
            response = self.post_json(
                reverse('hookup_verify_payment_api', args=[match.pk]),
                {'reference': 'forged-reference'},
            )
        self.assertEqual(response.status_code, 400)
        match.refresh_from_db()
        self.assertFalse(match.initiator_paid)
        self.assertFalse(PaymentTransaction.objects.filter(reference='forged-reference').exists())

    def test_support_bot_cache_and_resilient_fallback(self):
        ticket = SupportTicket.objects.create(user=self.alice)
        with patch('dating.services.support_bot._generate_answer', return_value='Use the in-app settings page.') as generate:
            first_answer = process_support_inquiry(ticket, '  How do I change settings? ')
            second_answer = process_support_inquiry(ticket, 'how do i change settings?')
        self.assertEqual(first_answer, 'Use the in-app settings page.')
        self.assertEqual(second_answer, first_answer)
        generate.assert_called_once()
        cached = SupportKnowledgeBase.objects.get()
        cached.refresh_from_db()
        self.assertFalse(cached.is_verified_by_admin)
        self.assertEqual(cached.hit_count, 2)

        fallback_ticket = SupportTicket.objects.create(user=self.bob)
        with override_settings(GEMINI_API_KEY=''), self.assertLogs('dating.services.support_bot', level='WARNING'):
            response = process_support_inquiry(fallback_ticket, 'Where is my payment?')
        fallback_ticket.refresh_from_db()
        self.assertEqual(response, FALLBACK_RESPONSE)
        self.assertEqual(fallback_ticket.status, 'open')

        escalated_ticket = SupportTicket.objects.create(user=self.bob)
        with override_settings(GEMINI_API_KEY=''), self.assertLogs('dating.services.support_bot', level='WARNING'):
            response = process_support_inquiry(
                escalated_ticket,
                'I need to speak to a human about my account being broken.',
            )
        escalated_ticket.refresh_from_db()
        self.assertEqual(escalated_ticket.status, 'needs_admin')
        self.assertIn('flagged your request for a support agent', response)
