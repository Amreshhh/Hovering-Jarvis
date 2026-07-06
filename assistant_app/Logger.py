class LogColors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    WARNING = "\033[93m"
    ERROR = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


def log_msg(msg, level="INFO"):
    if level == "INFO":
        print(f"{LogColors.BLUE}[*] {msg}{LogColors.ENDC}")
    elif level == "SUCCESS":
        print(f"{LogColors.GREEN}[+] {msg}{LogColors.ENDC}")
    elif level == "WARNING":
        print(f"{LogColors.WARNING}[!] {msg}{LogColors.ENDC}")
    elif level == "ERROR":
        print(f"{LogColors.ERROR}[X] {msg}{LogColors.ENDC}")
    elif level == "TRIGGER":
        print(f"\n{LogColors.HEADER}{LogColors.BOLD}>>> {msg}{LogColors.ENDC}")
