from django.contrib import admin

from dating.admin import ProfileAdmin
from .models import SexCallProfile


class SexCallProfileAdmin(ProfileAdmin):
    mode = 'SEX_CALL'

    def get_queryset(self, request):
        return super().get_queryset(request).filter(relationship_mode=self.mode)

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or ())
        exclude.append('relationship_mode')
        return exclude

    def save_model(self, request, obj, form, change):
        obj.relationship_mode = self.mode
        super().save_model(request, obj, form, change)


admin.site.register(SexCallProfile, SexCallProfileAdmin)
