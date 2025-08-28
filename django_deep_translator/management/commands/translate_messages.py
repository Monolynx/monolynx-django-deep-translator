import logging
import os
import time
from optparse import make_option

import polib
from django.conf import settings
from django.core.management.base import BaseCommand

from django_deep_translator.utils import get_translator

logger = logging.getLogger(__name__)

default_options = () if not hasattr(BaseCommand, 'option_list') \
    else BaseCommand.option_list


class Command(BaseCommand):
    help = ('autotranslate all the message files that have been generated '
            'using the `makemessages` command.')

    option_list = default_options + (
        make_option('--locale', '-l', default=[], dest='locale', action='append',
                    help='autotranslate the message files for the given locale(s) (e.g. pt_BR). '
                         'can be used multiple times.'),
        make_option('--untranslated', '-u', default=False, dest='skip_translated', action='store_true',
                    help='autotranslate the fuzzy and empty messages only.'),
        make_option('--set-fuzzy', '-f', default=False, dest='set_fuzzy', action='store_true',
                    help='set the fuzzy flag on autotranslated messages.'),
        make_option('--source-language', '-s', default='en', dest='source_language', action='store',
                    help='override the default source language (en) used for translation.'),
        make_option('--limit-translations', default=None, dest='limit_translations', type='int',
                    help='limit the number of translations to perform (default: no limit).'),
        make_option('--requests-per-10s', default=10, dest='requests_per_10s', type='int',
                    help='maximum number of translation requests per 10 seconds (default: 10).'),
    )

    def add_arguments(self, parser):
        # Previously, only the standard optparse library was supported and
        # you would have to extend the command option_list variable with optparse.make_option().
        # See: https://docs.djangoproject.com/en/1.8/howto/custom-management-commands/#accepting-optional-arguments
        # In django 1.8, these custom options can be added in the add_arguments()
        parser.add_argument('--locale', '-l', default=[], dest='locale', action='append',
                            help='autotranslate the message files for the given locale(s) (e.g. pt_BR). '
                                 'can be used multiple times.')
        parser.add_argument('--untranslated', '-u', default=False, dest='skip_translated', action='store_true',
                            help='autotranslate the fuzzy and empty messages only.')
        parser.add_argument('--set-fuzzy', '-f', default=False, dest='set_fuzzy', action='store_true',
                            help='set the fuzzy flag on autotranslated messages.')
        parser.add_argument('--source-language', '-s', default='en', dest='source_language', action='store',
                            help='override the default source language (en) used for translation.')
        parser.add_argument('--limit-translations', default=None, dest='limit_translations', type=int,
                            help='limit the number of translations to perform (default: no limit).')
        parser.add_argument('--requests-per-10s', default=10, dest='requests_per_10s', type=int,
                            help='maximum number of translation requests per 10 seconds (default: 10).')

    def set_options(self, **options):
        self.locale = options['locale']
        self.skip_translated = options['skip_translated']
        self.set_fuzzy = options['set_fuzzy']
        self.source_language = options['source_language']
        self.limit_translations = options['limit_translations']
        self.requests_per_10s = options['requests_per_10s']
        
        # Rate limiting variables
        self.translated_count = 0
        self.request_times = []

    def wait_for_rate_limit(self):
        """Wait if necessary to respect the requests per 10 seconds limit."""
        current_time = time.time()
        
        # Remove requests older than 10 seconds
        self.request_times = [t for t in self.request_times if current_time - t < 10]
        
        # If we've reached the limit, wait until we can make another request
        if len(self.request_times) >= self.requests_per_10s:
            sleep_time = 10 - (current_time - self.request_times[0])
            if sleep_time > 0:
                logger.info(f'Rate limit reached, waiting {sleep_time:.2f} seconds...')
                time.sleep(sleep_time)
                # Clean up old requests again after sleeping
                current_time = time.time()
                self.request_times = [t for t in self.request_times if current_time - t < 10]
        
        # Record this request
        self.request_times.append(current_time)

    def handle(self, *args, **options):
        self.set_options(**options)

        assert getattr(settings, 'USE_I18N', False), 'i18n framework is disabled'
        assert getattr(settings, 'LOCALE_PATHS', []), 'locale paths is not configured properly'
        
        if self.limit_translations:
            logger.info(f'Translation limit set to: {self.limit_translations}')
        logger.info(f'Rate limit: {self.requests_per_10s} requests per 10 seconds')
        
        for directory in settings.LOCALE_PATHS:
            # Check if we've reached the translation limit
            if self.limit_translations and self.translated_count >= self.limit_translations:
                logger.info(f'Translation limit of {self.limit_translations} reached. Stopping.')
                return
                
            # walk through all the paths and find all the pot files
            for root, dirs, files in os.walk(directory):
                for file in files:
                    # Check limit again in inner loop
                    if self.limit_translations and self.translated_count >= self.limit_translations:
                        logger.info(f'Translation limit of {self.limit_translations} reached. Stopping.')
                        return
                        
                    if not file.endswith('.po'):
                        # process file only if it is a .po file
                        continue

                    # get the target language from the parent folder name
                    target_language = os.path.basename(os.path.dirname(root))

                    if self.locale and target_language not in self.locale:
                        logger.info('skipping translation for locale `{}`'.format(target_language))
                        continue

                    # Pass the limit check to translate_file
                    if not self.translate_file(root, file, target_language):
                        # translate_file returns False when limit is reached
                        return

    def translate_file(self, root, file_name, target_language):
        """
        convenience method for translating a po file

        :param root:            the absolute path of folder where the file is present
        :param file_name:       name of the file to be translated (it should be a pot file)
        :param target_language: language in which the file needs to be translated
        :return:                True if completed, False if stopped due to limit
        """
        logger.info('filling up translations for locale `{}`'.format(target_language))

        po_file_path = os.path.join(root, file_name)
        po = polib.pofile(po_file_path)
        
        for entry in po:
            # Check translation limit
            if self.limit_translations and self.translated_count >= self.limit_translations:
                logger.info(f'Translation limit of {self.limit_translations} reached.')
                return False
                
            # skip translated
            if self.skip_translated and entry.translated():
                continue

            # Skip empty entries
            if not entry.msgid.strip():
                continue

            # Apply rate limiting before making translation request
            self.wait_for_rate_limit()
            
            try:
                translation = get_translator()
                entry.msgstr = translation.translate_string(text=entry.msgid, source_language=self.source_language,
                                                            target_language=target_language)
                if self.set_fuzzy:
                    entry.flags.append('fuzzy')

                # Increment counter and save file after each translation
                self.translated_count += 1
                po.save()
                
                logger.info(f'Translated entry #{self.translated_count}: "{entry.msgid[:50]}..." -> "{entry.msgstr[:50]}..."')
                
            except Exception as e:
                logger.error(f'Error translating "{entry.msgid[:50]}...": {e}')
                continue

        return True
