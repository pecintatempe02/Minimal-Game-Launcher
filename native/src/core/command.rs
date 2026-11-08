//! Command rules.
//!
//! MGL executes one shell command exactly as shown. The shell is invoked
//! explicitly (`/bin/sh -lc "<command>"`), so quoting, environment assignments,
//! wrappers and pipes work. No other part of the application parses or rewrites
//! command text.

/// The shell used for every launch.
pub const SHELL: &str = "/bin/sh";

/// Validate the command text itself.
pub fn validate_command(text: &str) -> Result<(), String> {
    if text.trim().is_empty() {
        return Err("The command is empty.".to_string());
    }
    if text.contains('\0') {
        return Err("The command contains a NUL byte.".to_string());
    }
    Ok(())
}

/// Build the arguments passed to the shell. `-l` makes login-shell `PATH`
/// additions visible, which is what makes `wine`, `gamescope` and `flatpak`
/// behave the same way they do in a terminal.
pub fn shell_argv(command: &str) -> [String; 3] {
    [SHELL.to_string(), "-lc".to_string(), command.to_string()]
}

/// Whether `text` contains shell syntax, used only for an editor hint.
pub fn has_shell_syntax(text: &str) -> bool {
    text.contains("&&")
        || text.contains("||")
        || text.contains('|')
        || text.contains('>')
        || text.contains('<')
        || text.contains(';')
        || text.contains("$(")
        || text.contains('`')
        || text.contains('*')
        || text.contains('&')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_are_passed_verbatim() {
        let command = "gamescope -f -- env WINEPREFIX=\"$HOME/p\" wine \"/games/A B/g.exe\"";
        assert_eq!(shell_argv(command)[2], command);
        assert!(validate_command(command).is_ok());
        assert!(validate_command("   ").is_err());
        assert!(validate_command("echo \0").is_err());
    }
}
