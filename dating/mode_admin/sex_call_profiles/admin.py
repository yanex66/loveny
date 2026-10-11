from django.contrib import admin

from dating.admin import ModeProfileAdmin
from .models import SexCallProfile


class SexCallProfileAdmin(ModeProfileAdmin):
    mode = 'SEX_CALL'


admin.site.register(SexCallProfile, SexCallProfileAdmin)
