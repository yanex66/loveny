from django.contrib import admin

from dating.admin import ModeProfileAdmin
from .models import HookupProfile


class HookupProfileAdmin(ModeProfileAdmin):
    mode = 'HOOKUP'


admin.site.register(HookupProfile, HookupProfileAdmin)
