# datasets.forms

import re
from django import forms
from django.db import models
from datasets.models import Dataset, Hit, DatasetFile
from main.choices import FORMATS, STATUS_FILE, HitRelationType
from places.models import PlaceDescription


class HitModelForm(forms.ModelForm):
    match = forms.CharField(
        initial='none',
        widget=forms.RadioSelect(choices=HitRelationType))

    class Meta:
        model = Hit
        fields = [
            'id', 'authority', 'authrecord_id', 'query_pass', 'score', 'json', 'relation_type'
        ]
        hidden_fields = [
            'id', 'authority', 'authrecord_id', 'query_pass', 'score', 'json', 'relation_type']
        widgets = {
            'id': forms.HiddenInput(),
            'authority': forms.HiddenInput(),
            'authrecord_id': forms.HiddenInput(),
            'json': forms.HiddenInput(),
            # 'relation_type': forms.TextInput(),
        }

    def __init__(self, *args, **kwargs):
        super(HitModelForm, self).__init__(*args, **kwargs)

        for key in self.fields:
            self.fields[key].required = False

        self.description = None
        if self.instance and self.instance.json:
            candidate_id = self.instance.json.get('place_id')

            try:
                # Fetch the single PlaceDescription entry for the extracted place_id
                self.description = PlaceDescription.objects.get(place_id=candidate_id)
            except PlaceDescription.DoesNotExist:
                pass


class DatasetFileModelForm(forms.ModelForm):
    class Meta:
        model = DatasetFile
        fields = ('dataset_id', 'file', 'rev', 'format', 'delimiter',
                  'df_status', 'header', 'numrows')

    def __init__(self, *args, **kwargs):
        super(DatasetFileModelForm, self).__init__(*args, **kwargs)
        for field in self.fields.values():
            field.error_messages = {'required': 'The field {fieldname} is required'.format(
                fieldname=field.label)}


SCRIPT_LANG = re.compile(r'^(Arab|Hebr|Syrc|Armn|Grek|Cyrl|Latn)=[a-z]{2,3}(-[A-Za-z0-9]{1,8})*$')


def clean_script_languages(value):
    """"Arab=ar, Latn=ar-Latn" -> "Arab=ar Latn=ar-Latn"; raises on bad entries."""
    parts = [p for p in re.split(r'[\s,;]+', (value or '').strip()) if p]
    bad = [p for p in parts if not SCRIPT_LANG.match(p)]
    if bad:
        raise forms.ValidationError(
            'Use script=language pairs, e.g. "Arab=ar Latn=ar-Latn"; scripts: Arab, Hebr, Syrc, Armn, Grek, '
            'Cyrl, Latn. Not understood: ' + ', '.join(bad))
    return ' '.join(parts) or None


class DatasetDetailModelForm(forms.ModelForm):
    class Meta:
        model = Dataset
        # file fields = ('file','rev','uri_base','format','dataset_id','delimiter',
        #   'status','accepted_date','header','numrows')
        fields = ('owner', 'creator', 'contributors', 'source', 'id', 'label', 'title', 'uri_base', 'description',
                  'citation', 'datatype', 'numlinked', 'public', 'webpage', 'license', 'record_group', 'script_languages', 'featured',
                  'image_file')
        widgets = {
            'description': forms.Textarea(attrs={
                'rows': 2, 'cols': 55, 'class': 'textarea', 'placeholder': 'Brief description'}),
            'citation': forms.Textarea(attrs={
                'rows': 2, 'cols': 55, 'class': 'textarea'}),
            'creator': forms.TextInput(attrs={'size': 50}),
            'source': forms.TextInput(attrs={'size': 50}),
            'contributors': forms.TextInput(attrs={'size': 50}),
            'webpage': forms.TextInput(attrs={'size': 50}),
            'license': forms.URLInput(attrs={'size': 50, 'list': 'license-options',
                                             'placeholder': 'Licence URL, e.g. https://creativecommons.org/licenses/by/4.0/'}),
            'record_group': forms.TextInput(attrs={'size': 20, 'placeholder': 'e.g. kima'}),
            'script_languages': forms.TextInput(attrs={'size': 40, 'placeholder': 'e.g. Arab=ar Latn=ar-Latn'}),
            # 'uri_base': forms.TextInput(attrs={'size': 50}),
            'uri_base': forms.URLInput(attrs={'size': 50}),
            'featured': forms.TextInput(attrs={'size': 4})
        }

    def clean_label(self):
        label = self.cleaned_data['label']
        if ' ' in label:
            print("there's a space goddamit")
            raise forms.ValidationError('label cannot contain any space')
        return label

    def clean_script_languages(self):
        return clean_script_languages(self.cleaned_data.get('script_languages'))

    file = forms.FileField(required=False)
    uri_base = forms.URLField(
        widget=forms.URLInput(
            attrs={'placeholder': 'Leave blank unless changed', 'size': 40}
        ),
        required=False
    )
    format = forms.ChoiceField(choices=FORMATS, initial="delimited")

    def __init__(self, *args, **kwargs):
        super(DatasetDetailModelForm, self).__init__(*args, **kwargs)
        for field in self.fields.values():
            field.error_messages = {'required': 'The field {fieldname} is required'.format(
                fieldname=field.label)}


class DatasetCreateModelForm(forms.ModelForm):
    file = forms.FileField()
    rev = forms.IntegerField()
    format = forms.ChoiceField(choices=FORMATS, widget=forms.RadioSelect, initial='delimited')
    delimiter = forms.CharField()
    header = forms.CharField()
    df_status = forms.ChoiceField(choices=STATUS_FILE)
    numrows = forms.IntegerField()
    upload_date = forms.DateTimeField()

    class Meta:
        model = Dataset
        fields = ('owner', 'id', 'title', 'label', 'datatype', 'description', 'uri_base', 'public',
                  'creator', 'contributors', 'source', 'webpage', 'license', 'record_group', 'script_languages', 'image_file', 'featured')
        widgets = {
            'description': forms.Textarea(
                attrs={'rows': 2, 'cols': 45, 'class': 'textarea', 'placeholder': 'Brief description'}),
            'uri_base': forms.URLInput(attrs={'placeholder': 'Leave blank unless record IDs are URIs', 'size': 45}),
            'title': forms.TextInput(attrs={'size': 45}),
            'label': forms.TextInput(attrs={'placeholder': '20 char max; no spaces', 'size': 22}),
            'creator': forms.TextInput(attrs={'size': 45}),
            'source': forms.TextInput(attrs={'size': 45}),
            'featured': forms.TextInput(attrs={'size': 4}),
            'webpage': forms.URLInput(attrs={'size': 45, 'placeholder': 'Project home page, if any'}),
            'license': forms.URLInput(attrs={'size': 45, 'list': 'license-options',
                                             'placeholder': 'Licence URL, e.g. https://creativecommons.org/licenses/by/4.0/'}),
            'record_group': forms.TextInput(attrs={'size': 20, 'placeholder': 'e.g. kima (optional)'}),
            'script_languages': forms.TextInput(attrs={'size': 40, 'placeholder': 'e.g. Arab=ar Latn=ar-Latn (optional)'})
        }

    def clean_label(self):
        label = self.cleaned_data['label']
        if ' ' in label:
            raise forms.ValidationError('Label cannot contain any spaces; replace with underscores (_)')
        return label

    def clean_script_languages(self):
        return clean_script_languages(self.cleaned_data.get('script_languages'))

    def __init__(self, *args, **kwargs):
        super(DatasetCreateModelForm, self).__init__(*args, **kwargs)
        for field in self.fields.values():
            field.error_messages = {'required': 'The field {fieldname} is required'.format(
                fieldname=field.label)}
