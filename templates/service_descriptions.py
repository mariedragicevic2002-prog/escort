"""

Service Descriptions - Comprehensive experience type descriptions.
Used when client asks about services, what's included, or the difference between experiences.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


from config import get_profile_url


import logging
logger = logging.getLogger("adella_chatbot.service_descriptions")

def get_gfe_description(include_profile_link: bool = True) -> str:
    """
    Get GFE (Girlfriend Experience) description.
    
    Args:
        include_profile_link: Whether to include profile link
        
    Returns:
        GFE service description
    """
    description = """GFE (Girlfriend Experience):
\u2022 Welcome drink on arrival
\u2022 Designer lingerie
\u2022 Sensual and erotic foreplay with deep french kissing
\u2022 Body touching, oil play, titty play, finger play, hand relief
\u2022 Erotic BJ (covered)
\u2022 DATY (if you wish) and/or mutual oral (69)
\u2022 Playful and passionate sex in multiple positions
\u2022 Multiple shots (MSOG)"""
    
    if include_profile_link:
        description += f"\n\nFor more details, check out my profile: {get_profile_url()}"
    
    return description


def get_dgfe_description(include_profile_link: bool = True) -> str:
    """
    Get DGFE (Dirty Girlfriend Experience) description.
    
    Args:
        include_profile_link: Whether to include profile link
        
    Returns:
        DGFE service description
    """
    try:
        from core.rates_from_config import get_dgfe_extra_over_gfe
        extra = get_dgfe_extra_over_gfe()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        extra = 100
    description = f"""DGFE (Dirty Girlfriend Experience) - GFE + ${extra}:
\u2022 All GFE services included PLUS:
\u2022 Erotic BBBJ (blowjob no condom)
\u2022 CIM or COB finish options"""
    
    if include_profile_link:
        description += f"\n\nFor more details, check out my profile: {get_profile_url()}"
    
    return description


def get_pse_description(include_profile_link: bool = True) -> str:
    """
    Get PSE (Pornstar Experience) description.
    
    Args:
        include_profile_link: Whether to include profile link
        
    Returns:
        PSE service description
    """
    description = """PSE (Pornstar Experience):
\u2022 Welcome drink on arrival
\u2022 All GFE and DGFE services included PLUS:
\u2022 Pornstar blowjob - epic deepthroat, facefucking, gagging, drooling, choking, spitting
\u2022 Truly wet and sloppy 'Yes daddy' cock worship session
\u2022 Multiple positions
\u2022 Rimming and anal play on you (at your request)
\u2022 COF, CIM, or CIMWS finish options
\u2022 ROUGHER DIRTIER SEX: dirty talk, slapping, spanking, choking, hair pulling
\u2022 Submissive and hardcore positions
\u2022 For the dominant/deviant type who enjoys wilder, dirtier, rougher play"""
    
    if include_profile_link:
        description += f"\n\nFor more details, check out my profile: {get_profile_url()}"
    
    return description


def get_all_experiences_description(include_profile_link: bool = True) -> str:
    """
    Get description of all experience types (difference between them).
    
    Args:
        include_profile_link: Whether to include profile link
        
    Returns:
        Complete service descriptions
    """
    description = f"""{get_gfe_description(include_profile_link=False)}

{get_dgfe_description(include_profile_link=False)}

{get_pse_description(include_profile_link=False)}"""
    
    if include_profile_link:
        description += f"\n\nFor more details, check out my profile: {get_profile_url()}"
    
    return description


