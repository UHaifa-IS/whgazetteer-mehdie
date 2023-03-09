# whg.builtins
from django import template
# from django.utils.html import escape
# from django.utils.safestring import mark_safe
import json

register = template.Library()


@register.filter(name='get')
def get(d, k):
    if d:
        jd = json.loads(d) or {}
    else:
        jd = {}
    return jd.get(k, None)
