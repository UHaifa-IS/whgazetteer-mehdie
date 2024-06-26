from django import forms
from django.db import models
from .models import Collection, CollectionLink

from tinymce.widgets import TinyMCE

class CollectionLinkForm(forms.ModelForm):
    class Meta:
        model = CollectionLink
        fields = ('collection', 'uri', 'label', 'link_type')
        widgets = {
            'uri': forms.URLInput,
            'label': forms.TextInput(attrs={'size': 50}),
            'link_type': forms.Select()
        }

    def __init__(self, *args, **kwargs):
        super(CollectionLinkForm, self).__init__(*args, **kwargs)

class CollectionModelForm(forms.ModelForm):
    # ** trying to return to referrer
    next = forms.CharField(required=False)
    # **

    class Meta:
        model = Collection
        fields = ('id','owner','title','collection_class','description','keywords','rel_keywords',
                  'image_file','file','datasets','creator','contact', 'webpage','public','featured' )

        widgets = {
            'title': forms.TextInput(attrs={'size': 45}),
            'keywords': forms.TextInput(attrs={'size': 45, 'placeholder':'comma-delimited'}),
            'rel_keywords': forms.TextInput(attrs={'size': 45, 'placeholder':'comma-delimited'}),
            'creator': forms.TextInput(attrs={'size': 45}),
            'contact': forms.TextInput(attrs={'size': 45}),
            'webpage': forms.TextInput(attrs={'size': 45}),
            'description': forms.Textarea(attrs={'rows':3,'cols': 45,'class':'textarea',
                'placeholder':'A single paragraph. Note that a PDF file of any length can be uploaded later as well.'}),
            'image_file':forms.FileInput(),
            'file':forms.FileInput(),
            'datasets': forms.CheckboxSelectMultiple,
            'featured': forms.TextInput(attrs={'size': 3}),
            # 'content': TinyMCE(attrs={'cols': 40, 'rows': 6})
        }

    def __init__(self, *args, **kwargs):
        super(CollectionModelForm, self).__init__(*args, **kwargs)
