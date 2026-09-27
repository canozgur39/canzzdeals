# CanzzDeals

A Python-based CSFloat listing monitor that automatically retrieves,
filters and tracks newly listed items, with optional Discord notifications
and CSV export.

## Features

- Automated monitoring of new CSFloat listings
- Configurable price and float filters
- Include/exclude keyword filtering
- Duplicate detection and persistent seen-item tracking
- Discord notifications with embedded listing information
- CSV export for collected listings
- Tkinter graphical user interface
- Selenium-based browser login and session handling
- Asynchronous HTTP requests using aiohttp
- Logging and error handling
- Exponential backoff for temporary errors
- Configurable refresh interval and monitoring options

## Technologies

- Python
- asyncio
- aiohttp
- BeautifulSoup
- Selenium
- Tkinter
- discord.py
- JSON
- CSV

## How It Works

1. The application opens a browser session for CSFloat authentication.
2. Session information is captured for subsequent requests.
3. The monitor periodically retrieves the newest listings.
4. Listings are parsed and converted into structured data.
5. Configurable filters are applied based on price, float and keywords.
6. Previously processed listings are ignored using persistent duplicate tracking.
7. Matching listings can be exported to CSV and sent to Discord.

## Project Structure

The application is organised into several components:

- Configuration and filtering
- Selenium session management
- CSFloat listing scraper
- Listing filtering and parsing
- Duplicate tracking
- Discord notification system
- CSV exporter
- Tkinter GUI
- Monitoring loop and logging

## What I Learned

This project gave me practical experience with:

- Asynchronous programming in Python
- HTTP requests and external web data
- Web scraping and HTML parsing
- Browser automation with Selenium
- Discord bot integration
- Data filtering and processing
- Persistent application state
- Logging and error handling
- Building a desktop GUI
- Combining multiple components into a single application

## Future Improvements

- Improve automated test coverage
- Separate components into individual modules
- Improve configuration and secret management
- Add more robust error handling
- Improve monitoring performance
- Add additional data sources
