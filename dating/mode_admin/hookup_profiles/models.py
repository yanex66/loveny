from dating.models import Profile


class HookupProfile(Profile):
    class Meta:
        proxy = True
        verbose_name = 'Hookup Profile'
        verbose_name_plural = 'Hookup Profiles'
