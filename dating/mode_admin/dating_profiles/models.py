from dating.models import Profile


class DatingProfile(Profile):
    class Meta:
        proxy = True
        verbose_name = 'Dating Profile'
        verbose_name_plural = 'Dating Profiles'
