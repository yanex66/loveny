from django.contrib import admin

from dating.admin import ModeProfileAdmin
from .models import DatingProfile


class DatingProfileAdmin(ModeProfileAdmin):
    mode = 'DATING'


admin.site.register(DatingProfile, DatingProfileAdmin)
