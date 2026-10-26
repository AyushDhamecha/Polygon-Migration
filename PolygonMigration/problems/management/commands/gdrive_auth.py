from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from google_auth_oauthlib.flow import InstalledAppFlow

from problems.storage.google_drive import SCOPES


class Command(BaseCommand):
    help = "Run the Google OAuth consent flow once and save the token to GOOGLE_DRIVE_TOKEN_FILE."

    def handle(self, *args, **options):
        client_file = settings.GOOGLE_DRIVE_CREDENTIALS_FILE
        token_file = settings.GOOGLE_DRIVE_TOKEN_FILE
        if not client_file or not token_file:
            raise CommandError("Set GOOGLE_DRIVE_CREDENTIALS_FILE and GOOGLE_DRIVE_TOKEN_FILE in .env first.")

        flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_file, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
        self.stdout.write(self.style.SUCCESS(f"Token saved to {token_file}"))
