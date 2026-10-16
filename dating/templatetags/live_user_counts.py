from django import template

from dating.admin_stats import get_user_counts

register = template.Library()


@register.simple_tag
def live_user_counts():
    return get_user_counts()
