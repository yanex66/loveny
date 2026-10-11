from dating.models import Profile


class SexCallProfile(Profile):
    class Meta:
        proxy = True
        verbose_name = 'Sex Call Profile'
        verbose_name_plural = 'Sex Call Profiles'
